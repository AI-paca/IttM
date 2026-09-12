//! Native Tesseract adapter. Handles are private, reused within one document,
//! and never shared between threads. Tesseract copies the borrowed RGB plane.
use anyhow::{Context, Result, bail, ensure};
use image::{Rgb, RgbImage, imageops};
use ittm_pipeline_core::ImagePlane;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::{CStr, CString, c_char, c_int, c_void},
    ptr,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct Recognition {
    pub text: String,
    pub words: Vec<Word>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Word {
    pub text: String,
    pub bbox: [u32; 4],
    pub confidence_ppm: u32,
}

unsafe extern "C" {
    fn TessBaseAPICreate() -> *mut c_void;
    fn TessBaseAPIDelete(api: *mut c_void);
    fn TessBaseAPIInit2(
        api: *mut c_void,
        path: *const c_char,
        language: *const c_char,
        oem: c_int,
    ) -> c_int;
    fn TessBaseAPISetPageSegMode(api: *mut c_void, psm: c_int);
    fn TessBaseAPISetVariable(api: *mut c_void, name: *const c_char, value: *const c_char)
    -> c_int;
    fn TessBaseAPISetImage(
        api: *mut c_void,
        image: *const u8,
        width: c_int,
        height: c_int,
        channels: c_int,
        stride: c_int,
    );
    fn TessBaseAPISetSourceResolution(api: *mut c_void, ppi: c_int);
    fn TessBaseAPIClearAdaptiveClassifier(api: *mut c_void);
    fn TessBaseAPIRecognize(api: *mut c_void, monitor: *mut c_void) -> c_int;
    fn TessBaseAPIGetTsvText(api: *mut c_void, page: c_int) -> *mut c_char;
    fn TessDeleteText(text: *const c_char);
    fn TessMonitorCreate() -> *mut c_void;
    fn TessMonitorDelete(monitor: *mut c_void);
    fn TessMonitorSetDeadlineMSecs(monitor: *mut c_void, deadline: c_int);
    fn TessMonitorSetCancelFunc(
        monitor: *mut c_void,
        callback: unsafe extern "C" fn(*mut c_void, c_int) -> bool,
    );
    fn TessMonitorSetCancelThis(monitor: *mut c_void, context: *mut c_void);
}
struct Api(*mut c_void);
impl Drop for Api {
    fn drop(&mut self) {
        unsafe {
            TessBaseAPIDelete(self.0);
        }
    }
}
struct Monitor(*mut c_void);
impl Drop for Monitor {
    fn drop(&mut self) {
        unsafe {
            TessMonitorDelete(self.0);
        }
    }
}
unsafe extern "C" fn cancelled(context: *mut c_void, _: c_int) -> bool {
    // SAFETY: the Arc-owned AtomicBool outlives the synchronous Recognize call.
    unsafe { &*context.cast::<AtomicBool>() }.load(Ordering::Relaxed)
}

pub struct Tesseract {
    handles: BTreeMap<String, Api>,
    pub languages: BTreeSet<String>,
    timeout: Duration,
    cancel: Arc<AtomicBool>,
}
impl Tesseract {
    pub fn new(timeout: Duration, cancel: Arc<AtomicBool>) -> Result<Self> {
        let output = crate::document::command_output(
            std::process::Command::new("tesseract").arg("--list-langs"),
            Duration::from_secs(10),
            &cancel,
        )?;
        let languages = String::from_utf8(output)?
            .lines()
            .skip(1)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect::<BTreeSet<_>>();
        ensure!(!languages.is_empty(), "No Tesseract traineddata installed");
        Ok(Self {
            handles: BTreeMap::new(),
            languages,
            timeout,
            cancel,
        })
    }

    fn handle(&mut self, language: &str) -> Result<*mut c_void> {
        if !self.handles.contains_key(language) {
            ensure!(
                self.handles.len() < 8,
                "Too many Tesseract language configurations"
            );
            let api = Api(unsafe { TessBaseAPICreate() });
            ensure!(!api.0.is_null(), "Could not allocate Tesseract");
            let path = std::env::var("TESSDATA_PREFIX")
                .ok()
                .map(CString::new)
                .transpose()?;
            let language_c = CString::new(language)?;
            // SAFETY: the API and C strings stay alive; each handle belongs to this worker.
            let status = unsafe {
                TessBaseAPIInit2(
                    api.0,
                    path.as_ref().map_or(ptr::null(), |p| p.as_ptr()),
                    language_c.as_ptr(),
                    1,
                )
            };
            ensure!(
                status == 0,
                "Could not initialize Tesseract language {language}"
            );
            unsafe {
                TessBaseAPISetVariable(api.0, c"user_defined_dpi".as_ptr(), c"300".as_ptr());
            }
            self.handles.insert(language.to_owned(), api);
        }
        Ok(self.handles[language].0)
    }

    pub fn recognize(
        &mut self,
        plane: &ImagePlane,
        language: &str,
        psm: i32,
    ) -> Result<Recognition> {
        ensure!(!self.cancel.load(Ordering::Relaxed), "Conversion cancelled");
        if language.split('+').any(|l| !self.languages.contains(l)) {
            return Ok(Recognition::default());
        }
        let mut result = self.once(plane, language, psm)?;
        // Same bounded recognition-miss retry as the former Python stage adapter.
        if result.words.is_empty() && matches!(psm, 4 | 6) && plane.height > 128 {
            let width = ((u64::from(plane.width) * 128 + u64::from(plane.height) / 2)
                / u64::from(plane.height))
            .max(1) as u32;
            let source = RgbImage::from_raw(plane.width, plane.height, plane.pixels.clone())
                .context("Invalid RGB raster")?;
            let resized = imageops::resize(&source, width, 128, imageops::FilterType::Lanczos3);
            let mut padded = RgbImage::from_pixel(width + 32, 160, Rgb([255; 3]));
            imageops::replace(&mut padded, &resized, 16, 16);
            result = self.once(&crate::document::rgb_plane(padded), language, psm)?;
            project_words(
                &mut result,
                [16, 16, width, 128],
                [plane.width, plane.height],
            );
        }
        Ok(result)
    }

    fn once(&mut self, plane: &ImagePlane, language: &str, psm: i32) -> Result<Recognition> {
        ensure!(
            plane.is_valid() && plane.format == ittm_pipeline_core::PixelFormat::Rgb8,
            "Tesseract requires a valid RGB plane"
        );
        ensure!(
            plane.width <= i32::MAX as u32
                && plane.height <= i32::MAX as u32
                && plane.stride <= i32::MAX as u32,
            "Raster exceeds Tesseract limits"
        );
        ensure!((0..=13).contains(&psm), "Invalid Tesseract PSM");
        let api = self.handle(language)?;
        let monitor = Monitor(unsafe { TessMonitorCreate() });
        ensure!(!monitor.0.is_null(), "Could not allocate OCR monitor");
        // SAFETY: all pointers refer to live allocations throughout this synchronous call.
        let status = unsafe {
            TessMonitorSetDeadlineMSecs(
                monitor.0,
                self.timeout.as_millis().min(i32::MAX as u128) as i32,
            );
            TessMonitorSetCancelFunc(monitor.0, cancelled);
            TessMonitorSetCancelThis(monitor.0, Arc::as_ptr(&self.cancel).cast_mut().cast());
            // Match independent CLI attempts: cache traineddata, not adaptive document state.
            TessBaseAPIClearAdaptiveClassifier(api);
            TessBaseAPISetPageSegMode(api, psm);
            TessBaseAPISetImage(
                api,
                plane.pixels.as_ptr(),
                plane.width as i32,
                plane.height as i32,
                3,
                plane.stride as i32,
            );
            TessBaseAPISetSourceResolution(api, 300);
            TessBaseAPIRecognize(api, monitor.0)
        };
        ensure!(!self.cancel.load(Ordering::Relaxed), "Conversion cancelled");
        ensure!(
            status == 0,
            "Tesseract failed or exceeded its recognition deadline"
        );
        let text = unsafe { TessBaseAPIGetTsvText(api, 0) };
        ensure!(!text.is_null(), "Tesseract returned no TSV");
        let value = unsafe { CStr::from_ptr(text) }.to_str().map(str::to_owned);
        unsafe {
            TessDeleteText(text);
        }
        parse_tsv(&value?, plane.width, plane.height)
    }
}

pub fn parse_tsv(value: &str, width: u32, height: u32) -> Result<Recognition> {
    let mut rows = Vec::new();
    for (order, row) in value.lines().enumerate() {
        if row.starts_with("level\t") || row.is_empty() {
            continue;
        }
        let cells = row.splitn(12, '\t').collect::<Vec<_>>();
        ensure!(cells.len() == 12, "Malformed Tesseract TSV");
        let text = cells[11].trim();
        if text.is_empty() {
            continue;
        }
        let number = |i: usize| cells[i].parse::<u32>().context("Malformed TSV coordinate");
        let (x, y, w, h) = (number(6)?, number(7)?, number(8)?, number(9)?);
        ensure!(w > 0 && h > 0, "Empty TSV word box");
        let bbox = [
            x.min(width),
            y.min(height),
            x.saturating_add(w).min(width),
            y.saturating_add(h).min(height),
        ];
        if bbox[2] <= bbox[0] || bbox[3] <= bbox[1] {
            continue;
        }
        let confidence = cells[10].parse::<f64>()?;
        ensure!(confidence.is_finite(), "Invalid OCR confidence");
        rows.push((
            [number(1)?, number(2)?, number(3)?, number(4)?],
            order,
            Word {
                text: text.to_owned(),
                bbox,
                confidence_ppm: (confidence.clamp(0.0, 100.0) * 10_000.0).round() as u32,
            },
        ));
    }
    rows.sort_by_key(|(line, order, word)| (*line, word.bbox[0], word.bbox[1], *order));
    let mut result = Recognition::default();
    let mut start = 0;
    while start < rows.len() {
        let mut stop = start + 1;
        while stop < rows.len() && rows[stop].0 == rows[start].0 {
            stop += 1;
        }
        let mut words = rows[start..stop]
            .iter()
            .map(|r| r.2.clone())
            .collect::<Vec<_>>();
        normalize_line(&mut words);
        if !result.text.is_empty() {
            result.text.push('\n');
        }
        result.text.push_str(
            &words
                .iter()
                .map(|w| w.text.as_str())
                .collect::<Vec<_>>()
                .join(" "),
        );
        result.words.extend(words);
        start = stop;
    }
    Ok(result)
}
fn normalize_line(words: &mut [Word]) {
    for i in 0..words.len() {
        let prev = i
            .checked_sub(1)
            .map(|n| words[n].text.to_lowercase())
            .unwrap_or_default();
        let next = words
            .get(i + 1)
            .map(|w| w.text.to_lowercase())
            .unwrap_or_default();
        let value = words[i].text.as_str();
        let replacement = if ["“|", "“l", "\"l", "‘l", "'l"].contains(&value)
            && ["already", "have"].contains(&next.as_str())
        {
            Some("\"I")
        } else if value == "Sh" && prev == "and" && next == "means" {
            Some("5h")
        } else if ["»", "•"].contains(&value) && ["|f", "|t", "if", "it"].contains(&next.as_str())
        {
            Some("-")
        } else if ["IT", "|T", "|f"].contains(&value) && next == "you" {
            Some(if prev == "-" { "If" } else { "- If" })
        } else if (["—", "—.", "–", "-."].contains(&value)
            && ["there", "safely"].contains(&prev.as_str()))
            || (value == "~." && next == "answer:")
        {
            Some("->")
        } else {
            None
        };
        if let Some(text) = replacement {
            words[i].text = text.into();
        }
    }
    if words.iter().any(|w| w.text.starts_with("\"I")) {
        for word in words {
            if let Some(base) = word.text.strip_suffix('”') {
                word.text = format!("{base}\"");
            }
        }
    }
}
fn project_words(result: &mut Recognition, content: [u32; 4], source: [u32; 2]) {
    let [x, y, width, height] = content;
    for word in &mut result.words {
        let [left, top, right, bottom] = word.bbox;
        let lower = |v: u32, offset: u32, scale: u32, size: u32| {
            ((u64::from(v.saturating_sub(offset)) * u64::from(size)) / u64::from(scale))
                .min(u64::from(size)) as u32
        };
        let upper = |v: u32, offset: u32, scale: u32, size: u32| {
            (u64::from(v.saturating_sub(offset)) * u64::from(size))
                .div_ceil(u64::from(scale))
                .min(u64::from(size)) as u32
        };
        word.bbox = [
            lower(left, x, width, source[0]),
            lower(top, y, height, source[1]),
            upper(right, x, width, source[0]),
            upper(bottom, y, height, source[1]),
        ];
    }
    result
        .words
        .retain(|w| w.bbox[2] > w.bbox[0] && w.bbox[3] > w.bbox[1]);
    if result.words.is_empty() {
        result.text.clear();
    }
}

/// EasyOCR remains an optional process; only pixels and word evidence cross its boundary.
pub struct EasyOcr {
    process: std::process::Child,
    requests: Option<std::sync::mpsc::SyncSender<String>>,
    replies: std::sync::mpsc::Receiver<std::result::Result<Recognition, String>>,
    io_thread: Option<std::thread::JoinHandle<()>>,
    timeout: Duration,
    cancel: Arc<AtomicBool>,
}
impl EasyOcr {
    pub fn start(timeout: Duration, cancel: Arc<AtomicBool>) -> Result<Self> {
        use std::{
            io::{BufRead, Read, Write},
            process::{Command, Stdio},
            sync::mpsc,
        };
        let python = std::env::var("ITTM_EASYOCR_PYTHON")
            .context("EasyOCR is optional: set ITTM_EASYOCR_PYTHON to its Python interpreter")?;
        let worker = std::env::var("ITTM_EASYOCR_WORKER").unwrap_or_else(|_| {
            format!("{}/../ocr/app/native_worker.py", env!("CARGO_MANIFEST_DIR"))
        });
        let mut process = Command::new(python)
            .arg(worker)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()?;
        let mut input = process.stdin.take().context("EasyOCR stdin unavailable")?;
        let mut output = std::io::BufReader::new(
            process
                .stdout
                .take()
                .context("EasyOCR stdout unavailable")?,
        );
        let (requests, incoming) = mpsc::sync_channel::<String>(1);
        let (responses, replies) = mpsc::sync_channel(1);
        let io_thread = std::thread::spawn(move || {
            for request in incoming {
                let result = (|| -> Result<Recognition> {
                    input.write_all(request.as_bytes())?;
                    input.write_all(b"\n")?;
                    input.flush()?;
                    let mut response = String::new();
                    output
                        .by_ref()
                        .take(8 * 1024 * 1024 + 1)
                        .read_line(&mut response)?;
                    ensure!(
                        response.len() <= 8 * 1024 * 1024 && response.ends_with('\n'),
                        "Invalid or oversized EasyOCR response"
                    );
                    let value: serde_json::Value = serde_json::from_str(&response)?;
                    if let Some(error) = value.get("error") {
                        bail!("EasyOCR: {error}");
                    }
                    Ok(serde_json::from_value(value)?)
                })();
                let failed = result.is_err();
                if responses.send(result.map_err(|e| e.to_string())).is_err() || failed {
                    break;
                }
            }
        });
        Ok(Self {
            process,
            requests: Some(requests),
            replies,
            io_thread: Some(io_thread),
            timeout,
            cancel,
        })
    }
    pub fn recognize(&mut self, plane: &ImagePlane) -> Result<Recognition> {
        use base64::Engine;
        ensure!(!self.cancel.load(Ordering::Relaxed), "Conversion cancelled");
        let request = serde_json::to_string(
            &serde_json::json!({"width":plane.width,"height":plane.height,
            "rgb":base64::engine::general_purpose::STANDARD.encode(&plane.pixels)}),
        )?;
        self.requests
            .as_ref()
            .context("EasyOCR stopped")?
            .send(request)?;
        let started = std::time::Instant::now();
        loop {
            if self.cancel.load(Ordering::Relaxed) || started.elapsed() >= self.timeout {
                let _ = self.process.kill();
                bail!("EasyOCR cancelled or exceeded its recognition deadline");
            }
            match self.replies.recv_timeout(Duration::from_millis(25)) {
                Ok(result) => {
                    let result = result.map_err(anyhow::Error::msg)?;
                    ensure!(
                        result.words.iter().all(|w| w.bbox[0] < w.bbox[2]
                            && w.bbox[1] < w.bbox[3]
                            && w.bbox[2] <= plane.width
                            && w.bbox[3] <= plane.height
                            && w.confidence_ppm <= 1_000_000),
                        "Invalid EasyOCR word evidence"
                    );
                    return Ok(result);
                }
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                Err(error) => return Err(error.into()),
            }
        }
    }
}
impl Drop for EasyOcr {
    fn drop(&mut self) {
        self.requests.take();
        let _ = self.process.kill();
        let _ = self.process.wait();
        if let Some(thread) = self.io_thread.take() {
            let _ = thread.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tsv_keeps_line_order_confidence_and_local_syntax() {
        let tsv = "5\t1\t1\t1\t1\t2\t30\t1\t20\t8\t98.25\tyou\n5\t1\t1\t1\t1\t1\t0\t0\t20\t10\t97\t|f\n5\t1\t1\t1\t2\t1\t0\t20\t30\t10\t90\tanswer\n";
        let r = parse_tsv(tsv, 100, 50).unwrap();
        assert_eq!(r.text, "- If you\nanswer");
        assert_eq!(r.words[1].confidence_ppm, 982500);
        assert_eq!(r.words[2].bbox, [0, 20, 30, 30]);
    }
    #[test]
    fn malformed_tsv_is_not_silently_accepted() {
        assert!(parse_tsv("5\tbad", 100, 100).is_err());
    }
}
