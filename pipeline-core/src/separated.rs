use std::collections::BTreeMap;
use std::slice;
use std::str;
use std::sync::{Mutex, OnceLock};

const ALL_STAGE_MASK: u32 = (1 << 8) - 1;
const PLANNED_STAGE_MASK: u32 = (1 << 5) - 1;
const MAX_JOBS: usize = 512;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Rect {
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct OcrJob {
    rect: Rect,
    object_id: u32,
    row: u32,
    column: u32,
    row_span: u32,
    column_span: u32,
    recognition_mode: u32,
}

#[derive(Debug)]
struct JobRaster {
    width: u32,
    height: u32,
    stride: u32,
    pixels: Vec<u8>,
}

#[derive(Debug)]
struct Session {
    jobs: Vec<OcrJob>,
    job_rasters: Vec<JobRaster>,
    text: Vec<Option<String>>,
    confidence_milli: Vec<u32>,
    rendered: Option<Vec<u8>>,
    stage_mask: u32,
}

fn render_job_raster(
    pixels: &[u8],
    stride: usize,
    channels: usize,
    rect: Rect,
) -> JobRaster {
    let crop_width = (rect.right - rect.left) as usize;
    let crop_height = (rect.bottom - rect.top) as usize;
    let border = (crop_width.max(crop_height) / 80).clamp(8, 64);
    let width = crop_width + border * 2;
    let height = crop_height + border * 2;
    let output_stride = width * 3;
    let mut output = vec![255_u8; output_stride * height];

    for source_y in rect.top as usize..rect.bottom as usize {
        for source_x in rect.left as usize..rect.right as usize {
            let source = source_y * stride + source_x * channels;
            let target_x = source_x - rect.left as usize + border;
            let target_y = source_y - rect.top as usize + border;
            let target = target_y * output_stride + target_x * 3;
            match channels {
                1 => output[target..target + 3].fill(pixels[source]),
                3 => output[target..target + 3].copy_from_slice(&pixels[source..source + 3]),
                4 => {
                    let alpha = u16::from(pixels[source + 3]);
                    for channel in 0..3 {
                        let value = u16::from(pixels[source + channel]);
                        output[target + channel] =
                            ((value * alpha + 255 * (255 - alpha)) / 255) as u8;
                    }
                }
                _ => unreachable!("pixel format was validated"),
            }
        }
    }

    JobRaster {
        width: width as u32,
        height: height as u32,
        stride: output_stride as u32,
        pixels: output,
    }
}

#[derive(Default)]
struct Registry {
    next_handle: u32,
    sessions: BTreeMap<u32, Session>,
}

fn registry() -> &'static Mutex<Registry> {
    static REGISTRY: OnceLock<Mutex<Registry>> = OnceLock::new();
    REGISTRY.get_or_init(|| Mutex::new(Registry::default()))
}

fn channels(format: u32) -> Option<usize> {
    match format {
        1 => Some(1),
        3 => Some(3),
        4 => Some(4),
        _ => None,
    }
}

fn luminance(pixels: &[u8], offset: usize, channels: usize) -> u8 {
    if channels == 1 {
        return pixels[offset];
    }
    let red = u32::from(pixels[offset]);
    let green = u32::from(pixels[offset + 1]);
    let blue = u32::from(pixels[offset + 2]);
    ((red * 299 + green * 587 + blue * 114) / 1_000) as u8
}

fn channel_distance(
    pixels: &[u8],
    first: usize,
    second: usize,
    channels: usize,
) -> u8 {
    if channels == 1 {
        return pixels[first].abs_diff(pixels[second]);
    }
    (0..3)
        .map(|channel| pixels[first + channel].abs_diff(pixels[second + channel]))
        .max()
        .unwrap_or(0)
}

fn local_contrast_mask(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> Vec<bool> {
    const EDGE_DELTA: u8 = 28;
    let mut mask = vec![false; width * height];
    if width < 3 || height < 3 {
        return mask;
    }

    for y in 1..height - 1 {
        let row = y * stride;
        let previous_row = (y - 1) * stride;
        let next_row = (y + 1) * stride;
        for x in 1..width - 1 {
            let center = row + x * channels;
            let horizontal = channel_distance(
                pixels,
                row + (x - 1) * channels,
                row + (x + 1) * channels,
                channels,
            );
            let vertical = channel_distance(
                pixels,
                previous_row + x * channels,
                next_row + x * channels,
                channels,
            );
            let luminance_delta = luminance(pixels, center, channels)
                .abs_diff(luminance(pixels, row + (x - 1) * channels, channels))
                .max(
                    luminance(pixels, center, channels)
                        .abs_diff(luminance(pixels, previous_row + x * channels, channels)),
                );
            mask[y * width + x] =
                horizontal.max(vertical).max(luminance_delta) >= EDGE_DELTA;
        }
    }

    // Long table rules are topology evidence, not text. If they remain in
    // the foreground mask, every row of a ruled or coloured table becomes a
    // single page-sized OCR block.
    let mut column_counts = vec![0_usize; width];
    for row in mask.chunks_exact(width) {
        for (x, active) in row.iter().copied().enumerate() {
            column_counts[x] += usize::from(active);
        }
    }
    let rule_threshold = (height / 3).max(8);
    let mut rule_columns = vec![false; width];
    for (x, count) in column_counts.into_iter().enumerate() {
        if count < rule_threshold {
            continue;
        }
        let first = x.saturating_sub(1);
        let last = (x + 1).min(width - 1);
        rule_columns[first..=last].fill(true);
    }
    for row in mask.chunks_exact_mut(width) {
        for (x, active) in row.iter_mut().enumerate() {
            if rule_columns[x] {
                *active = false;
            }
        }
    }
    mask
}

fn row_bands(active: &[bool], bridge_gap: usize) -> Vec<(usize, usize)> {
    let mut bands = Vec::new();
    let mut start = None;
    let mut last_active = 0;
    for (row, is_active) in active.iter().copied().enumerate() {
        if is_active {
            start.get_or_insert(row);
            last_active = row;
        } else if let Some(first) = start
            && row.saturating_sub(last_active) > bridge_gap
        {
            bands.push((first, last_active + 1));
            start = None;
        }
    }
    if let Some(first) = start {
        bands.push((first, last_active + 1));
    }
    bands
}

fn plan_jobs(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> Vec<OcrJob> {
    let foreground = local_contrast_mask(pixels, width, height, stride, channels);
    let minimum_row_ink = (width / 700).max(3);
    let maximum_row_ink = (width * 3 / 5).max(minimum_row_ink + 1);
    let mut active_rows = vec![false; height];
    for (y, active) in active_rows.iter_mut().enumerate() {
        let count = foreground[y * width..(y + 1) * width]
            .iter()
            .filter(|value| **value)
            .count();
        *active = count >= minimum_row_ink && count <= maximum_row_ink;
    }

    let bridge_gap = (height / 1_200).clamp(1, 4);
    let mut bands = row_bands(&active_rows, bridge_gap);
    bands.retain(|(top, bottom)| bottom.saturating_sub(*top) >= 2);
    if bands.is_empty() {
        return Vec::new();
    }

    let raw_median_height = {
        let mut heights: Vec<usize> = bands.iter().map(|(top, bottom)| bottom - top).collect();
        heights.sort_unstable();
        heights[heights.len() / 2].max(1)
    };
    let glyph_gap = (raw_median_height / 3).clamp(1, 5);
    let mut lines: Vec<(usize, usize)> = Vec::with_capacity(bands.len());
    for (top, bottom) in bands {
        if let Some(previous) = lines.last_mut()
            && top.saturating_sub(previous.1) <= glyph_gap
        {
            previous.1 = bottom;
        } else {
            lines.push((top, bottom));
        }
    }
    let median_height = {
        let mut heights: Vec<usize> = lines.iter().map(|(top, bottom)| bottom - top).collect();
        heights.sort_unstable();
        heights[heights.len() / 2].max(1)
    };
    let object_gap = (median_height * 3).max(12);
    let x_padding = (width / 800).clamp(2, 12);
    let y_padding = (height / 1200).clamp(1, 6);
    let mut objects: Vec<Vec<(usize, usize)>> = Vec::new();
    for line in lines {
        let begins_object = objects.last().is_none_or(|object| {
            line.0
                .saturating_sub(object.last().expect("object is non-empty").1)
                > object_gap
        });
        if begins_object {
            objects.push(Vec::new());
        }
        objects.last_mut().expect("object was inserted").push(line);
    }

    const MAX_CONTEXT_LINES: usize = 16;
    let estimated_jobs = objects
        .iter()
        .map(|object| object.len().div_ceil(MAX_CONTEXT_LINES))
        .sum::<usize>();
    let mut jobs = Vec::with_capacity(estimated_jobs.min(MAX_JOBS));
    let mut source_row = 0_u32;
    for (object_id, object) in objects.iter().enumerate() {
        for chunk in object.chunks(MAX_CONTEXT_LINES) {
            let band_top = chunk[0].0;
            let band_bottom = chunk[chunk.len() - 1].1;
            let mut left = width;
            let mut right = 0;
            for y in band_top..band_bottom {
                for x in 0..width {
                    if foreground[y * width + x] {
                        left = left.min(x);
                        right = right.max(x + 1);
                    }
                }
            }
            if left >= right {
                source_row += chunk.len() as u32;
                continue;
            }
            jobs.push(OcrJob {
                rect: Rect {
                    left: left.saturating_sub(x_padding) as u32,
                    top: band_top.saturating_sub(y_padding) as u32,
                    right: (right + x_padding).min(width) as u32,
                    bottom: (band_bottom + y_padding).min(height) as u32,
                },
                object_id: object_id as u32,
                row: source_row,
                column: 0,
                row_span: chunk.len() as u32,
                column_span: 1,
                recognition_mode: recognition_mode_for_rect(Rect {
                    left: left.saturating_sub(x_padding) as u32,
                    top: band_top.saturating_sub(y_padding) as u32,
                    right: (right + x_padding).min(width) as u32,
                    bottom: (band_bottom + y_padding).min(height) as u32,
                }),
            });
            source_row += chunk.len() as u32;
            if jobs.len() == MAX_JOBS {
                return jobs;
            }
        }
    }
    jobs
}

fn recognition_mode_for_rect(rect: Rect) -> u32 {
    let width = rect.right - rect.left;
    let height = rect.bottom - rect.top;
    if width >= 2_200
        && height >= 1_500
        && u64::from(height) * 10 <= u64::from(width) * 9
    {
        2 // sparse text
    } else if width >= 1_600 && height >= 1_000 {
        1 // document
    } else {
        0 // ordinary text region
    }
}

fn render_session(session: &Session) -> Vec<u8> {
    let mut markdown = String::new();
    let mut previous_object = None;
    for (index, job) in session.jobs.iter().enumerate() {
        let Some(text) = session.text[index].as_deref().map(str::trim) else {
            continue;
        };
        if text.is_empty() {
            continue;
        }
        if !markdown.is_empty() {
            if previous_object == Some(job.object_id) {
                markdown.push('\n');
            } else {
                markdown.push_str("\n\n");
            }
        }
        markdown.push_str(text);
        previous_object = Some(job.object_id);
    }
    markdown.into_bytes()
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_alloc(length: u32) -> *mut u8 {
    if length == 0 {
        return std::ptr::null_mut();
    }
    let mut bytes = Vec::<u8>::with_capacity(length as usize);
    let pointer = bytes.as_mut_ptr();
    std::mem::forget(bytes);
    pointer
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_dealloc(pointer: *mut u8, capacity: u32) {
    if pointer.is_null() || capacity == 0 {
        return;
    }
    // SAFETY: callers only return pointers and capacities obtained from ittm_alloc.
    unsafe { drop(Vec::from_raw_parts(pointer, 0, capacity as usize)) };
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_begin(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    format: u32,
) -> u32 {
    let Some(channel_count) = channels(format) else {
        return 0;
    };
    let minimum_stride = match (width as usize).checked_mul(channel_count) {
        Some(value) => value,
        None => return 0,
    };
    let expected_length = match (stride as usize).checked_mul(height as usize) {
        Some(value) => value,
        None => return 0,
    };
    if pixels.is_null()
        || width == 0
        || height == 0
        || (stride as usize) < minimum_stride
        || pixel_length as usize != expected_length
    {
        return 0;
    }
    // SAFETY: the validated byte range is owned by the caller for this call.
    let input = unsafe { slice::from_raw_parts(pixels, expected_length) };
    let jobs = plan_jobs(
        input,
        width as usize,
        height as usize,
        stride as usize,
        channel_count,
    );
    let job_rasters = jobs
        .iter()
        .map(|job| render_job_raster(input, stride as usize, channel_count, job.rect))
        .collect();
    let job_count = jobs.len();
    let session = Session {
        jobs,
        job_rasters,
        text: vec![None; job_count],
        confidence_milli: vec![0; job_count],
        rendered: None,
        stage_mask: PLANNED_STAGE_MASK,
    };
    let Ok(mut registry) = registry().lock() else {
        return 0;
    };
    registry.next_handle = registry.next_handle.wrapping_add(1).max(1);
    let handle = registry.next_handle;
    registry.sessions.insert(handle, session);
    handle
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_job_raster_field(
    handle: u32,
    index: u32,
    field: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(raster) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.job_rasters.get(index as usize))
    else {
        return -1;
    };
    match field {
        0 => raster.width as i32,
        1 => raster.height as i32,
        2 => raster.stride as i32,
        3 => 3,
        _ => -1,
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_job_raster_length(handle: u32, index: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.job_rasters.get(index as usize))
                .map(|raster| raster.pixels.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_job_raster_copy(
    handle: u32,
    index: u32,
    output: *mut u8,
    capacity: u32,
) -> i32 {
    if output.is_null() && capacity > 0 {
        return -1;
    }
    let Ok(registry) = registry().lock() else {
        return -2;
    };
    let Some(raster) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.job_rasters.get(index as usize))
    else {
        return -3;
    };
    if (capacity as usize) < raster.pixels.len() {
        return -4;
    }
    if !raster.pixels.is_empty() {
        // SAFETY: capacity was validated and the source/destination do not overlap.
        unsafe {
            std::ptr::copy_nonoverlapping(
                raster.pixels.as_ptr(),
                output,
                raster.pixels.len(),
            )
        };
    }
    raster.pixels.len() as i32
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_job_count(handle: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .map(|session| session.jobs.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_job_field(handle: u32, index: u32, field: u32) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(job) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.jobs.get(index as usize))
    else {
        return -1;
    };
    match field {
        0 => job.rect.left as i32,
        1 => job.rect.top as i32,
        2 => job.rect.right as i32,
        3 => job.rect.bottom as i32,
        4 => job.object_id as i32,
        5 => job.row as i32,
        6 => job.column as i32,
        7 => job.row_span as i32,
        8 => job.column_span as i32,
        9 => job.recognition_mode as i32,
        _ => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_set_ocr(
    handle: u32,
    index: u32,
    text_pointer: *const u8,
    text_length: u32,
    confidence_milli: u32,
) -> i32 {
    if text_pointer.is_null() && text_length > 0 {
        return -1;
    }
    let text_bytes = if text_length == 0 {
        &[][..]
    } else {
        // SAFETY: the caller retains a valid UTF-8 byte range for this call.
        unsafe { slice::from_raw_parts(text_pointer, text_length as usize) }
    };
    let Ok(text) = str::from_utf8(text_bytes) else {
        return -2;
    };
    let Ok(mut registry) = registry().lock() else {
        return -3;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return -4;
    };
    let Some(slot) = session.text.get_mut(index as usize) else {
        return -5;
    };
    *slot = Some(text.to_owned());
    session.confidence_milli[index as usize] = confidence_milli.min(1_000);
    session.rendered = None;
    0
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_render_length(handle: u32) -> u32 {
    let Ok(mut registry) = registry().lock() else {
        return 0;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return 0;
    };
    let rendered = render_session(session);
    let length = rendered.len() as u32;
    session.rendered = Some(rendered);
    session.stage_mask = ALL_STAGE_MASK;
    length
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_render_copy(
    handle: u32,
    output: *mut u8,
    capacity: u32,
) -> i32 {
    if output.is_null() && capacity > 0 {
        return -1;
    }
    let Ok(registry) = registry().lock() else {
        return -2;
    };
    let Some(rendered) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.rendered.as_ref())
    else {
        return -3;
    };
    if (capacity as usize) < rendered.len() {
        return -4;
    }
    if !rendered.is_empty() {
        // SAFETY: capacity was validated and the source/destination do not overlap.
        unsafe { std::ptr::copy_nonoverlapping(rendered.as_ptr(), output, rendered.len()) };
    }
    rendered.len() as i32
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_stage_mask(handle: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .map(|session| session.stage_mask)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_drop(handle: u32) -> i32 {
    registry()
        .lock()
        .ok()
        .and_then(|mut registry| registry.sessions.remove(&handle))
        .map(|_| 0)
        .unwrap_or(-1)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn white_page_with_two_lines() -> (Vec<u8>, u32, u32) {
        let width = 80_u32;
        let height = 40_u32;
        let mut pixels = vec![255_u8; (width * height) as usize];
        for y in 7..11 {
            for x in 8..55 {
                pixels[(y * width + x) as usize] = 0;
            }
        }
        for y in 24..28 {
            for x in 12..70 {
                pixels[(y * width + x) as usize] = 0;
            }
        }
        (pixels, width, height)
    }

    #[test]
    fn plans_the_same_explicit_stage_boundary_for_raster_lines() {
        let (pixels, width, height) = white_page_with_two_lines();
        let jobs = plan_jobs(&pixels, width as usize, height as usize, width as usize, 1);
        assert_eq!(jobs.len(), 1);
        assert!(jobs[0].rect.left <= 8);
        assert!(jobs[0].rect.top <= 7);
        assert!(jobs[0].rect.right >= 70);
        assert!(jobs[0].rect.bottom >= 28);
        assert_ne!(
            jobs[0].rect,
            Rect {
                left: 0,
                top: 0,
                right: width,
                bottom: height,
            }
        );
        assert_eq!(jobs[0].row_span, 2);
    }

    #[test]
    fn native_ffi_runs_all_eight_stages_and_renders_in_source_order() {
        let (pixels, width, height) = white_page_with_two_lines();
        // SAFETY: the source byte slice remains alive for the call.
        let handle = unsafe {
            ittm_separated_begin(
                pixels.as_ptr(),
                pixels.len() as u32,
                width,
                height,
                width,
                1,
            )
        };
        assert_ne!(handle, 0);
        assert_eq!(ittm_separated_job_count(handle), 1);
        assert_eq!(ittm_separated_stage_mask(handle), PLANNED_STAGE_MASK);
        let text = "first\nsecond";
        // SAFETY: the string remains alive for the call.
        assert_eq!(
            unsafe {
                ittm_separated_set_ocr(handle, 0, text.as_ptr(), text.len() as u32, 900)
            },
            0,
        );
        let length = ittm_separated_render_length(handle);
        let mut output = vec![0_u8; length as usize];
        // SAFETY: output owns exactly the reported capacity.
        assert_eq!(
            unsafe { ittm_separated_render_copy(handle, output.as_mut_ptr(), length) },
            length as i32,
        );
        assert_eq!(String::from_utf8(output).unwrap(), "first\nsecond");
        assert_eq!(ittm_separated_stage_mask(handle), ALL_STAGE_MASK);
        assert_eq!(ittm_separated_drop(handle), 0);
    }

    #[test]
    fn dark_pages_use_the_same_foreground_planner() {
        let width = 60;
        let height = 20;
        let mut pixels = vec![12_u8; width * height];
        for y in 6..10 {
            for x in 9..45 {
                pixels[y * width + x] = 240;
            }
        }
        let jobs = plan_jobs(&pixels, width, height, width, 1);
        assert_eq!(jobs.len(), 1);
        assert!(jobs[0].rect.left <= 9);
        assert!(jobs[0].rect.right >= 45);
    }
}
