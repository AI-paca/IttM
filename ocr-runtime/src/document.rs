use anyhow::{Context, Result, bail, ensure};
use image::{DynamicImage, ImageDecoder, ImageReader, RgbImage};
use ittm_pipeline_core::{ImagePlane, PdfTextGeometryItem, PixelFormat, build_native_pdf_artifact};
use quick_xml::{
    Reader,
    events::{BytesStart, Event},
};
use std::{
    fs::{self, File},
    io::{Read, Seek, SeekFrom},
    path::Path,
    process::{Command, Stdio},
    sync::atomic::{AtomicBool, Ordering},
    thread,
    time::{Duration, Instant},
};

#[derive(Clone, Debug)]
pub struct Limits {
    pub upload_bytes: u64,
    pub image_pixels: u64,
    pub pdf_pages: usize,
    pub pdf_dimension: u32,
    pub command_timeout: Duration,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            upload_bytes: 128 * 1024 * 1024,
            image_pixels: 80_000_000,
            pdf_pages: 100,
            pdf_dimension: 6000,
            command_timeout: Duration::from_secs(60),
        }
    }
}
impl Limits {
    pub fn from_env() -> Result<Self> {
        fn value(name: &str, default: u64) -> Result<u64> {
            std::env::var(name)
                .ok()
                .map(|v| v.parse().with_context(|| format!("Invalid {name}")))
                .unwrap_or(Ok(default))
        }
        let mut limits = Self::default();
        limits.upload_bytes = value("OCR_MAX_UPLOAD_BYTES", limits.upload_bytes)?;
        limits.image_pixels = value("OCR_MAX_DECODED_IMAGE_PIXELS", limits.image_pixels)?;
        limits.pdf_pages = usize::try_from(value("OCR_MAX_PDF_PAGES", limits.pdf_pages as u64)?)?;
        limits.pdf_dimension = u32::try_from(value(
            "OCR_MAX_PDF_RENDER_DIMENSION",
            limits.pdf_dimension as u64,
        )?)?;
        limits.command_timeout = Duration::from_secs(value("OCR_COMMAND_TIMEOUT_SECONDS", 60)?);
        ensure!(
            limits.image_pixels > 0
                && limits.pdf_pages > 0
                && limits.pdf_dimension > 0
                && !limits.command_timeout.is_zero(),
            "Limits must be positive"
        );
        Ok(limits)
    }
}
pub fn rgb_plane(image: RgbImage) -> ImagePlane {
    ImagePlane {
        width: image.width(),
        height: image.height(),
        stride: image.width() * 3,
        format: PixelFormat::Rgb8,
        pixels: image.into_raw(),
    }
}
pub fn decode(path: &Path, limits: &Limits) -> Result<ImagePlane> {
    let reader = ImageReader::open(path)?.with_guessed_format()?;
    let mut decoder = reader.into_decoder()?;
    let (width, height) = decoder.dimensions();
    ensure!(
        u64::from(width) * u64::from(height) <= limits.image_pixels,
        "Decoded image exceeds pixel limit"
    );
    let mut allocation = image::Limits::default();
    allocation.max_alloc = Some(limits.image_pixels.saturating_mul(8));
    decoder.set_limits(allocation)?;
    let orientation = decoder.orientation()?;
    let mut decoded = DynamicImage::from_decoder(decoder)?;
    decoded.apply_orientation(orientation);
    let rgba = decoded.into_rgba8();
    let rgb = RgbImage::from_fn(rgba.width(), rgba.height(), |x, y| {
        let p = rgba.get_pixel(x, y);
        let a = u32::from(p[3]);
        image::Rgb(std::array::from_fn(|i| {
            ((u32::from(p[i]) * a + 255 * (255 - a) + 127) / 255) as u8
        }))
    });
    Ok(rgb_plane(rgb))
}

/// Spool child output to files, so neither pipe backpressure nor unbounded RAM
/// can prevent timeout/cancellation from killing and reaping the child.
pub fn command_output(
    command: &mut Command,
    timeout: Duration,
    cancel: &AtomicBool,
) -> Result<Vec<u8>> {
    let mut stdout = tempfile::tempfile()?;
    let mut stderr = tempfile::tempfile()?;
    let mut child = command
        .stdin(Stdio::null())
        .stdout(stdout.try_clone()?)
        .stderr(stderr.try_clone()?)
        .spawn()?;
    let start = Instant::now();
    let status = loop {
        if cancel.load(Ordering::Relaxed)
            || start.elapsed() >= timeout
            || stdout.metadata()?.len() > 64 * 1024 * 1024
            || stderr.metadata()?.len() > 1024 * 1024
        {
            let _ = child.kill();
            let _ = child.wait();
            bail!("External OCR/PDF command cancelled or exceeded its resource limit");
        }
        if let Some(status) = child.try_wait()? {
            break status;
        }
        thread::sleep(Duration::from_millis(10));
    };
    if !status.success() {
        stderr.seek(SeekFrom::Start(0))?;
        let mut detail = String::new();
        stderr.take(4096).read_to_string(&mut detail)?;
        bail!("External OCR/PDF command failed: {}", detail.trim());
    }
    ensure!(
        stdout.metadata()?.len() <= 64 * 1024 * 1024,
        "External command output exceeds limit"
    );
    stdout.seek(SeekFrom::Start(0))?;
    let mut output = Vec::new();
    stdout.read_to_end(&mut output)?;
    Ok(output)
}

pub fn is_pdf(path: &Path, filename: &str) -> Result<bool> {
    let mut magic = [0; 5];
    let n = File::open(path)?.read(&mut magic)?;
    Ok((n == 5 && &magic == b"%PDF-") || filename.to_ascii_lowercase().ends_with(".pdf"))
}
pub fn page_count(path: &Path, limits: &Limits, cancel: &AtomicBool) -> Result<usize> {
    let bytes = command_output(
        Command::new("pdfinfo").arg(path).env("LC_ALL", "C"),
        limits.command_timeout,
        cancel,
    )?;
    let text = String::from_utf8(bytes)?;
    let pages = text
        .lines()
        .find_map(|line| line.strip_prefix("Pages:"))
        .context("PDF has no page count")?
        .trim()
        .parse::<usize>()?;
    ensure!(
        pages > 0 && pages <= limits.pdf_pages,
        "PDF has {pages} pages; limit is {}",
        limits.pdf_pages
    );
    Ok(pages)
}
pub fn render_page(
    path: &Path,
    page: usize,
    limits: &Limits,
    cancel: &AtomicBool,
) -> Result<ImagePlane> {
    let directory = tempfile::tempdir()?;
    let prefix = directory.path().join("page");
    let n = page.to_string();
    let info = command_output(
        Command::new("pdfinfo")
            .args(["-f", &n, "-l", &n])
            .arg(path)
            .env("LC_ALL", "C"),
        limits.command_timeout,
        cancel,
    )?;
    let size = String::from_utf8(info)?
        .lines()
        .find(|line| line.contains("size:") && line.contains("pts"))
        .map(|line| {
            line.split("size:")
                .nth(1)
                .unwrap_or("")
                .split_whitespace()
                .filter_map(|v| v.parse::<f64>().ok())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let max_points = size.iter().copied().fold(0.0, f64::max);
    let dpi = if max_points * 300.0 / 72.0 > f64::from(limits.pdf_dimension) {
        (f64::from(limits.pdf_dimension) * 72.0 / max_points)
            .floor()
            .max(10.0) as u32
    } else {
        300
    };
    let mut command = Command::new("pdftoppm");
    command.args([
        "-f",
        &n,
        "-l",
        &n,
        "-singlefile",
        "-png",
        "-r",
        &dpi.to_string(),
    ]);
    if max_points == 0.0 || max_points * f64::from(dpi) / 72.0 > f64::from(limits.pdf_dimension) {
        command.args(["-scale-to", &limits.pdf_dimension.to_string()]);
    }
    command.arg(path).arg(&prefix);
    command_output(&mut command, limits.command_timeout, cancel)?;
    decode(&prefix.with_extension("png"), limits)
}

#[derive(Default)]
pub struct TextPage {
    pub markdown: String,
    pub tables: usize,
    pub cells: usize,
}
pub fn text_page(
    path: &Path,
    page: usize,
    records: bool,
    limits: &Limits,
    cancel: &AtomicBool,
) -> Result<Option<TextPage>> {
    let n = page.to_string();
    let raw = command_output(
        Command::new("pdftotext")
            .args(["-layout", "-f", &n, "-l", &n])
            .arg(path)
            .arg("-"),
        limits.command_timeout,
        cancel,
    )?;
    let text = String::from_utf8_lossy(&raw)
        .replace('\u{000c}', "")
        .trim_end()
        .to_owned();
    if !usable_text(&text) {
        return Ok(None);
    }
    if records {
        return Ok(Some(TextPage {
            markdown: text,
            ..Default::default()
        }));
    }
    let geometry = command_output(
        Command::new("pdftotext")
            .args(["-bbox-layout", "-f", &n, "-l", &n])
            .arg(path)
            .arg("-"),
        limits.command_timeout,
        cancel,
    );
    if let Ok(geometry) = geometry {
        if let Ok(items) = parse_bbox(&String::from_utf8_lossy(&geometry)) {
            if let Ok(Some(artifact)) = build_native_pdf_artifact(&items) {
                if !artifact.assembled.markdown.is_empty() {
                    let tables = artifact
                        .objects
                        .iter()
                        .filter(|o| {
                            !matches!(o.kind, ittm_pipeline_core::StructuralObjectKind::Paragraph)
                        })
                        .collect::<Vec<_>>();
                    return Ok(Some(TextPage {
                        markdown: artifact.assembled.markdown.clone(),
                        tables: tables.len(),
                        cells: tables.iter().map(|o| o.cells.len()).sum(),
                    }));
                }
            }
        }
    }
    Ok(Some(TextPage {
        markdown: text,
        ..Default::default()
    }))
}
fn usable_text(text: &str) -> bool {
    let compact = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let letter =
        |c: char| c.is_ascii_alphabetic() || ('А'..='я').contains(&c) || c == 'Ё' || c == 'ё';
    let words = compact
        .split(|c: char| !letter(c))
        .filter(|s| s.chars().count() >= 3)
        .count();
    let latin = compact
        .split(|c: char| !c.is_ascii_alphabetic())
        .filter(|s| s.len() >= 4)
        .collect::<Vec<_>>();
    let suspicious = latin
        .iter()
        .filter(|word| {
            word.chars()
                .skip(1)
                .take(word.len() - 2)
                .filter(|c| c.is_uppercase())
                .count()
                >= 2
                && word.chars().filter(|c| c.is_lowercase()).count() >= 2
        })
        .count();
    compact.chars().count() >= 200
        && words >= 20
        && !(latin.len() >= 12 && suspicious as f64 / latin.len() as f64 >= 0.28)
}
fn attribute(element: &BytesStart<'_>, key: &[u8]) -> Result<f64> {
    for item in element.attributes() {
        let item = item?;
        if item.key.as_ref() == key {
            let value = std::str::from_utf8(&item.value)?.parse::<f64>()?;
            ensure!(value.is_finite(), "Non-finite PDF coordinate");
            return Ok(value);
        }
    }
    bail!("Missing PDF geometry attribute")
}
fn parse_bbox(xml: &str) -> Result<Vec<PdfTextGeometryItem>> {
    let mut reader = Reader::from_str(xml);
    let mut items = Vec::new();
    let mut height = 0.0;
    let mut line_text = Vec::new();
    let mut line_box = [
        f64::INFINITY,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::NEG_INFINITY,
    ];
    let mut in_word = false;
    let mut word = String::new();
    loop {
        match reader.read_event()? {
            Event::Start(e) if e.local_name().as_ref() == b"page" => {
                height = attribute(&e, b"height")?;
                ensure!(height > 0.0, "Invalid PDF page height");
            }
            Event::Start(e) if e.local_name().as_ref() == b"line" => {
                line_text.clear();
                line_box = [
                    f64::INFINITY,
                    f64::INFINITY,
                    f64::NEG_INFINITY,
                    f64::NEG_INFINITY,
                ];
            }
            Event::Start(e) if e.local_name().as_ref() == b"word" => {
                in_word = true;
                word.clear();
                line_box[0] = line_box[0].min(attribute(&e, b"xMin")?);
                line_box[1] = line_box[1].min(attribute(&e, b"yMin")?);
                line_box[2] = line_box[2].max(attribute(&e, b"xMax")?);
                line_box[3] = line_box[3].max(attribute(&e, b"yMax")?);
            }
            Event::Text(e) if in_word => {
                word.push_str(&e.decode()?);
            }
            Event::GeneralRef(e) if in_word => {
                let name = e.decode()?;
                let escaped = format!("&{name};");
                word.push_str(&quick_xml::escape::unescape(&escaped)?);
            }
            Event::End(e) if e.local_name().as_ref() == b"word" => {
                in_word = false;
                if !word.trim().is_empty() {
                    line_text.push(word.trim().to_owned());
                }
            }
            Event::End(e) if e.local_name().as_ref() == b"line" && !line_text.is_empty() => {
                items.push(PdfTextGeometryItem {
                    text: line_text.join(" "),
                    width: line_box[2] - line_box[0],
                    height: Some(line_box[3] - line_box[1]),
                    transform: [1.0, 0.0, 0.0, 1.0, line_box[0], height - line_box[1]],
                });
            }
            Event::Eof => break,
            _ => {}
        }
    }
    Ok(items)
}
pub fn validate_file(path: &Path, limits: &Limits) -> Result<()> {
    let size = fs::metadata(path)?.len();
    ensure!(size > 0, "Uploaded file is empty");
    ensure!(
        limits.upload_bytes == 0 || size <= limits.upload_bytes,
        "File exceeds upload limit"
    );
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bbox_preserves_entities_and_pdf_coordinate_system() {
        let items=parse_bbox("<doc><page height=\"200\"><line><word xMin=\"10\" yMin=\"20\" xMax=\"30\" yMax=\"40\">A&amp;B</word></line></page></doc>").unwrap();
        assert_eq!(items[0].text, "A&B");
        assert_eq!(items[0].transform[5], 180.0);
        assert_eq!(items[0].width, 20.0);
    }
    #[test]
    fn text_gate_rejects_short_and_noisy_layers() {
        assert!(!usable_text("Hello"));
        assert!(usable_text(
            &"A readable document contains ordinary English words and useful information. "
                .repeat(6)
        ));
        assert!(!usable_text(&"aBCdeFgh ".repeat(40)));
    }
    #[test]
    fn transparent_pixels_are_white_and_dimensions_are_checked() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("image.png");
        image::RgbaImage::from_pixel(4, 3, image::Rgba([0, 0, 0, 0]))
            .save(&path)
            .unwrap();
        let result = decode(&path, &Limits::default()).unwrap();
        assert!(result.pixels.iter().all(|v| *v == 255));
        let limits = Limits {
            image_pixels: 10,
            ..Limits::default()
        };
        assert!(decode(&path, &limits).is_err());
    }
}
