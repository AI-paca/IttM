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
}

#[derive(Debug)]
struct Session {
    jobs: Vec<OcrJob>,
    text: Vec<Option<String>>,
    confidence_milli: Vec<u32>,
    rendered: Option<Vec<u8>>,
    stage_mask: u32,
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

fn background_luminance(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> u8 {
    let mut samples = Vec::new();
    let x_step = (width / 256).max(1);
    let y_step = (height / 256).max(1);
    for x in (0..width).step_by(x_step) {
        samples.push(luminance(pixels, x * channels, channels));
        samples.push(luminance(
            pixels,
            (height - 1) * stride + x * channels,
            channels,
        ));
    }
    for y in (0..height).step_by(y_step) {
        samples.push(luminance(pixels, y * stride, channels));
        samples.push(luminance(
            pixels,
            y * stride + (width - 1) * channels,
            channels,
        ));
    }
    samples.sort_unstable();
    samples[samples.len() / 2]
}

fn is_ink(value: u8, background: u8) -> bool {
    const DELTA: u8 = 24;
    if background >= 128 {
        value.saturating_add(DELTA) < background
    } else {
        value > background.saturating_add(DELTA)
    }
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
    let background = background_luminance(pixels, width, height, stride, channels);
    let minimum_row_ink = (width / 400).max(2);
    let mut active_rows = vec![false; height];

    for (y, active) in active_rows.iter_mut().enumerate() {
        let mut count = 0;
        let row_offset = y * stride;
        for x in 0..width {
            if is_ink(
                luminance(pixels, row_offset + x * channels, channels),
                background,
            ) {
                count += 1;
                if count >= minimum_row_ink {
                    *active = true;
                    break;
                }
            }
        }
    }

    let bridge_gap = (height / 900).clamp(1, 8);
    let mut bands = row_bands(&active_rows, bridge_gap);
    if bands.len() > MAX_JOBS {
        let group_size = bands.len().div_ceil(MAX_JOBS);
        bands = bands
            .chunks(group_size)
            .map(|chunk| (chunk[0].0, chunk[chunk.len() - 1].1))
            .collect();
    }
    if bands.is_empty() {
        return Vec::new();
    }

    let median_height = {
        let mut heights: Vec<usize> = bands.iter().map(|(top, bottom)| bottom - top).collect();
        heights.sort_unstable();
        heights[heights.len() / 2].max(1)
    };
    let object_gap = (median_height * 2).max(8);
    let x_padding = (width / 800).clamp(2, 12);
    let y_padding = (height / 1200).clamp(1, 6);
    let mut object_id = 0_u32;
    let mut previous_bottom = None;
    let mut jobs = Vec::with_capacity(bands.len());

    for (row, (band_top, band_bottom)) in bands.into_iter().enumerate() {
        if previous_bottom.is_some_and(|bottom| band_top.saturating_sub(bottom) > object_gap) {
            object_id += 1;
        }
        previous_bottom = Some(band_bottom);

        let mut left = width;
        let mut right = 0;
        for y in band_top..band_bottom {
            let row_offset = y * stride;
            for x in 0..width {
                if is_ink(
                    luminance(pixels, row_offset + x * channels, channels),
                    background,
                ) {
                    left = left.min(x);
                    right = right.max(x + 1);
                }
            }
        }
        if left >= right {
            continue;
        }
        jobs.push(OcrJob {
            rect: Rect {
                left: left.saturating_sub(x_padding) as u32,
                top: band_top.saturating_sub(y_padding) as u32,
                right: (right + x_padding).min(width) as u32,
                bottom: (band_bottom + y_padding).min(height) as u32,
            },
            object_id,
            row: row as u32,
            column: 0,
            row_span: 1,
            column_span: 1,
        });
    }
    jobs
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
    let job_count = jobs.len();
    let session = Session {
        jobs,
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
        assert_eq!(jobs.len(), 2);
        assert_eq!(
            jobs[0].rect,
            Rect {
                left: 6,
                top: 6,
                right: 57,
                bottom: 12
            }
        );
        assert_eq!(jobs[1].row, 1);
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
        assert_eq!(ittm_separated_job_count(handle), 2);
        assert_eq!(ittm_separated_stage_mask(handle), PLANNED_STAGE_MASK);
        for (index, text) in ["first", "second"].into_iter().enumerate() {
            // SAFETY: each string remains alive for the call.
            assert_eq!(
                unsafe {
                    ittm_separated_set_ocr(
                        handle,
                        index as u32,
                        text.as_ptr(),
                        text.len() as u32,
                        900,
                    )
                },
                0,
            );
        }
        let length = ittm_separated_render_length(handle);
        let mut output = vec![0_u8; length as usize];
        // SAFETY: output owns exactly the reported capacity.
        assert_eq!(
            unsafe { ittm_separated_render_copy(handle, output.as_mut_ptr(), length) },
            length as i32,
        );
        assert_eq!(String::from_utf8(output).unwrap(), "first\n\nsecond");
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
