use std::collections::BTreeMap;
use std::slice;
use std::str;
use std::sync::{Mutex, OnceLock};

const ALL_STAGE_MASK: u32 = (1 << 8) - 1;
const PLANNED_STAGE_MASK: u32 = (1 << 5) - 1;
const MAX_JOBS: usize = 512;
const OBJECT_PARAGRAPH: u32 = 0;
const OBJECT_LIST: u32 = 1;
const OBJECT_TABLE: u32 = 2;

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
    object_kind: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct DetectedLine {
    left: usize,
    top: usize,
    right: usize,
    bottom: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct DetectedObject {
    kind: u32,
    lines: Vec<DetectedLine>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct EdgeRule {
    position_start: usize,
    position_end: usize,
    span_start: usize,
    span_end: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct TableRegion {
    left: usize,
    top: usize,
    right: usize,
    bottom: usize,
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

fn longest_dense_span(active: &[bool], maximum_gap: usize) -> Option<(usize, usize)> {
    let mut best = None;
    let mut start = None;
    let mut last_active = 0;
    for (index, value) in active.iter().copied().enumerate() {
        if value {
            start.get_or_insert(index);
            last_active = index;
        } else if let Some(first) = start
            && index.saturating_sub(last_active) > maximum_gap
        {
            let candidate = (first, last_active + 1);
            if best.is_none_or(|current: (usize, usize)| {
                candidate.1 - candidate.0 > current.1 - current.0
            }) {
                best = Some(candidate);
            }
            start = None;
        }
    }
    if let Some(first) = start {
        let candidate = (first, last_active + 1);
        if best.is_none_or(|current: (usize, usize)| {
            candidate.1 - candidate.0 > current.1 - current.0
        }) {
            best = Some(candidate);
        }
    }
    best
}

fn horizontal_edge_rules(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> Vec<EdgeRule> {
    const RULE_DELTA: u8 = 18;
    let mut rows: Vec<EdgeRule> = Vec::new();
    for y in 1..height.saturating_sub(1) {
        let mut active = vec![false; width];
        for (x, value) in active.iter_mut().enumerate() {
            let center = y * stride + x * channels;
            let previous = (y - 1) * stride + x * channels;
            let next = (y + 1) * stride + x * channels;
            *value = channel_distance(pixels, center, previous, channels)
                .max(channel_distance(pixels, center, next, channels))
                >= RULE_DELTA;
        }
        let Some((left, right)) = longest_dense_span(&active, 2) else {
            continue;
        };
        if (right - left) * 100 < width * 45 {
            continue;
        }
        if let Some(previous) = rows.last_mut()
            && y <= previous.position_end + 1
            && right.min(previous.span_end).saturating_sub(left.max(previous.span_start)) * 100
                >= (right - left).min(previous.span_end - previous.span_start) * 80
        {
            previous.position_end = y + 1;
            previous.span_start = previous.span_start.min(left);
            previous.span_end = previous.span_end.max(right);
        } else {
            rows.push(EdgeRule {
                position_start: y,
                position_end: y + 1,
                span_start: left,
                span_end: right,
            });
        }
    }
    rows
}

fn vertical_edge_rules(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> Vec<EdgeRule> {
    const RULE_DELTA: u8 = 18;
    let mut columns: Vec<EdgeRule> = Vec::new();
    for x in 1..width.saturating_sub(1) {
        let mut active = vec![false; height];
        for (y, value) in active.iter_mut().enumerate() {
            let center = y * stride + x * channels;
            let previous = y * stride + (x - 1) * channels;
            let next = y * stride + (x + 1) * channels;
            *value = channel_distance(pixels, center, previous, channels)
                .max(channel_distance(pixels, center, next, channels))
                >= RULE_DELTA;
        }
        let Some((top, bottom)) = longest_dense_span(&active, 2) else {
            continue;
        };
        if (bottom - top) * 100 < height * 25 {
            continue;
        }
        if let Some(previous) = columns.last_mut()
            && x <= previous.position_end + 1
            && bottom.min(previous.span_end).saturating_sub(top.max(previous.span_start)) * 100
                >= (bottom - top).min(previous.span_end - previous.span_start) * 80
        {
            previous.position_end = x + 1;
            previous.span_start = previous.span_start.min(top);
            previous.span_end = previous.span_end.max(bottom);
        } else {
            columns.push(EdgeRule {
                position_start: x,
                position_end: x + 1,
                span_start: top,
                span_end: bottom,
            });
        }
    }
    columns
}

fn table_regions(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> Vec<TableRegion> {
    let horizontal = horizontal_edge_rules(pixels, width, height, stride, channels);
    let vertical = vertical_edge_rules(pixels, width, height, stride, channels);
    let maximum_gap = (height / 8).max(48);
    let mut groups: Vec<Vec<EdgeRule>> = Vec::new();
    for rule in horizontal {
        let joins_previous = groups.last().and_then(|group| group.last()).is_some_and(|previous| {
            let overlap = rule.span_end.min(previous.span_end)
                .saturating_sub(rule.span_start.max(previous.span_start));
            let minimum_width = (rule.span_end - rule.span_start)
                .min(previous.span_end - previous.span_start);
            rule.position_start.saturating_sub(previous.position_end) <= maximum_gap
                && overlap * 100 >= minimum_width * 80
        });
        if joins_previous {
            groups.last_mut().expect("group exists").push(rule);
        } else {
            groups.push(vec![rule]);
        }
    }

    let mut regions = Vec::new();
    for group in groups.into_iter().filter(|group| group.len() >= 3) {
        let first = group.first().expect("group is non-empty");
        let last = group.last().expect("group is non-empty");
        let left = group.iter().map(|rule| rule.span_start).min().unwrap_or(0);
        let right = group.iter().map(|rule| rule.span_end).max().unwrap_or(width);
        let horizontal_top = first.position_start;
        let horizontal_bottom = last.position_end;
        let matching_vertical: Vec<EdgeRule> = vertical
            .iter()
            .copied()
            .filter(|rule| {
                let x = (rule.position_start + rule.position_end) / 2;
                let overlap = rule.span_end.min(horizontal_bottom)
                    .saturating_sub(rule.span_start.max(horizontal_top));
                left <= x
                    && x <= right
                    && overlap * 100
                        >= horizontal_bottom.saturating_sub(horizontal_top) * 60
            })
            .collect();
        let (top, bottom) = if matching_vertical.len() >= 2 {
            (
                matching_vertical
                    .iter()
                    .map(|rule| rule.span_start)
                    .min()
                    .unwrap_or(horizontal_top),
                matching_vertical
                    .iter()
                    .map(|rule| rule.span_end)
                    .max()
                    .unwrap_or(horizontal_bottom),
            )
        } else {
            let mut steps: Vec<usize> = group
                .windows(2)
                .map(|pair| pair[1].position_start.saturating_sub(pair[0].position_start))
                .filter(|step| *step > 0)
                .collect();
            steps.sort_unstable();
            let cadence = steps.get(steps.len() / 2).copied().unwrap_or(0);
            (
                horizontal_top.saturating_sub(cadence),
                (horizontal_bottom + cadence).min(height),
            )
        };
        regions.push(TableRegion {
            left,
            top,
            right,
            bottom,
        });
    }
    regions.sort_by_key(|region| (region.top, region.left));
    let mut merged: Vec<TableRegion> = Vec::new();
    for region in regions {
        if let Some(previous) = merged.last_mut()
            && region.top <= previous.bottom
            && region.left.max(previous.left) < region.right.min(previous.right)
        {
            previous.left = previous.left.min(region.left);
            previous.top = previous.top.min(region.top);
            previous.right = previous.right.max(region.right);
            previous.bottom = previous.bottom.max(region.bottom);
        } else {
            merged.push(region);
        }
    }
    merged
}

fn detected_line(
    foreground: &[bool],
    width: usize,
    top: usize,
    bottom: usize,
) -> Option<DetectedLine> {
    let mut left = width;
    let mut right = 0;
    for y in top..bottom {
        for x in 0..width {
            if foreground[y * width + x] {
                left = left.min(x);
                right = right.max(x + 1);
            }
        }
    }
    (left < right).then_some(DetectedLine {
        left,
        top,
        right,
        bottom,
    })
}

fn line_has_list_marker(line: DetectedLine, foreground: &[bool], width: usize) -> bool {
    let mut active_columns = vec![false; line.right - line.left];
    for y in line.top..line.bottom {
        for x in line.left..line.right {
            active_columns[x - line.left] |= foreground[y * width + x];
        }
    }
    let mut runs = Vec::new();
    let mut start = None;
    for (index, active) in active_columns.iter().copied().enumerate() {
        if active {
            start.get_or_insert(index);
        } else if let Some(first) = start.take() {
            runs.push((first, index));
        }
    }
    if let Some(first) = start {
        runs.push((first, active_columns.len()));
    }
    let height = line.bottom - line.top;
    runs.windows(2).any(|pair| {
        pair[0].1 - pair[0].0 <= height * 2
            && pair[1].0.saturating_sub(pair[0].1) >= height.max(4)
    })
}

fn remove_page_edge_artifacts(
    lines: &mut Vec<DetectedLine>,
    regions: &[TableRegion],
    foreground: &[bool],
    width: usize,
    height: usize,
) {
    const MIN_REPEATED_EDGE_COMPONENTS: usize = 6;
    let edge_slack = (width / 20).max(2);
    let narrow_edge_width = (width / 8).max(1);
    let near_left = |line: &DetectedLine| {
        line.left <= edge_slack && line.right.saturating_sub(line.left) <= narrow_edge_width
    };
    let near_right = |line: &DetectedLine| {
        line.right.saturating_add(edge_slack) >= width
            && line.right.saturating_sub(line.left) <= narrow_edge_width
    };
    let repeated_left = lines.iter().filter(|line| near_left(line)).count()
        >= MIN_REPEATED_EDGE_COMPONENTS;
    let repeated_right = lines.iter().filter(|line| near_right(line)).count()
        >= MIN_REPEATED_EDGE_COMPONENTS;
    let frame_margin = (height / 8).max(2);
    let maximum_frame_height = (height / 80).max(8);
    let maximum_structure_height = (height / 20).max(8);

    lines.retain(|line| {
        let inside_table = line_in_table_region(*line, regions);
        let line_width = line.right.saturating_sub(line.left);
        let line_height = line.bottom.saturating_sub(line.top);
        let covered_columns = line_covered_columns(*line, foreground, width);
        let repeated_binding = !inside_table
            && ((repeated_left && near_left(line))
                || (repeated_right && near_right(line)));
        let horizontal_structure = !inside_table
            && line_height <= maximum_structure_height
            && line_width * 2 >= width
            && line_width >= line_height.saturating_mul(20)
            && covered_columns * 100 >= line_width * 85
            && line_component_count(*line, foreground, width, 4) <= 4;
        let horizontal_page_frame = horizontal_structure
            || (!inside_table
                && line_height <= maximum_frame_height
                && line_width * 2 >= width
                && line_width >= line_height.saturating_mul(50)
                && (line.top <= frame_margin
                    || line.bottom.saturating_add(frame_margin) >= height));
        !repeated_binding && !horizontal_page_frame
    });
}

fn line_in_table_region(line: DetectedLine, regions: &[TableRegion]) -> bool {
    let center_x = (line.left + line.right) / 2;
    let center_y = (line.top + line.bottom) / 2;
    regions.iter().any(|region| {
        region.left <= center_x
            && center_x <= region.right
            && region.top <= center_y
            && center_y <= region.bottom
    })
}

fn line_ink_count(line: DetectedLine, foreground: &[bool], width: usize) -> usize {
    (line.top..line.bottom)
        .map(|y| {
            foreground[y * width + line.left..y * width + line.right]
                .iter()
                .filter(|value| **value)
                .count()
        })
        .sum()
}

fn line_covered_columns(line: DetectedLine, foreground: &[bool], width: usize) -> usize {
    (line.left..line.right)
        .filter(|x| {
            (line.top..line.bottom).any(|y| foreground[y * width + *x])
        })
        .count()
}

fn line_component_count(
    line: DetectedLine,
    foreground: &[bool],
    width: usize,
    stop_after: usize,
) -> usize {
    let local_width = line.right - line.left;
    let local_height = line.bottom - line.top;
    let mut visited = vec![false; local_width * local_height];
    let mut stack = Vec::new();
    let mut components = 0;

    for local_y in 0..local_height {
        for local_x in 0..local_width {
            let local_index = local_y * local_width + local_x;
            let source_index = (line.top + local_y) * width + line.left + local_x;
            if visited[local_index] || !foreground[source_index] {
                continue;
            }
            components += 1;
            if components > stop_after {
                return components;
            }
            visited[local_index] = true;
            stack.push((local_x, local_y));
            while let Some((x, y)) = stack.pop() {
                if x > 0 {
                    let next = y * local_width + x - 1;
                    let source = (line.top + y) * width + line.left + x - 1;
                    if !visited[next] && foreground[source] {
                        visited[next] = true;
                        stack.push((x - 1, y));
                    }
                }
                if x + 1 < local_width {
                    let next = y * local_width + x + 1;
                    let source = (line.top + y) * width + line.left + x + 1;
                    if !visited[next] && foreground[source] {
                        visited[next] = true;
                        stack.push((x + 1, y));
                    }
                }
                if y > 0 {
                    let next = (y - 1) * local_width + x;
                    let source = (line.top + y - 1) * width + line.left + x;
                    if !visited[next] && foreground[source] {
                        visited[next] = true;
                        stack.push((x, y - 1));
                    }
                }
                if y + 1 < local_height {
                    let next = (y + 1) * local_width + x;
                    let source = (line.top + y + 1) * width + line.left + x;
                    if !visited[next] && foreground[source] {
                        visited[next] = true;
                        stack.push((x, y + 1));
                    }
                }
            }
        }
    }
    components
}

fn remove_repeated_edge_foreground(
    foreground: &mut [bool],
    regions: &[TableRegion],
    width: usize,
    height: usize,
) {
    const MIN_REPEATED_EDGE_COMPONENTS: usize = 6;
    let strip_width = (width / 16).clamp(4, width / 2);
    let maximum_component_height = (height / 20).max(8);
    let mut repeated = [false; 2];

    for (side, repeated_side) in repeated.iter_mut().enumerate() {
        let (outer_start, outer_end, inner_start, inner_end) = if side == 0 {
            (0, strip_width, strip_width, strip_width * 2)
        } else {
            (
                width - strip_width,
                width,
                width.saturating_sub(strip_width * 2),
                width - strip_width,
            )
        };
        let detached_rows: Vec<bool> = foreground
            .chunks_exact(width)
            .map(|row| {
                row[outer_start..outer_end].iter().any(|value| *value)
                    && !row[inner_start..inner_end].iter().any(|value| *value)
            })
            .collect();
        *repeated_side = row_bands(&detached_rows, 2)
            .into_iter()
            .filter(|(top, bottom)| {
                let component_height = bottom - top;
                component_height >= 2 && component_height <= maximum_component_height
            })
            .count()
            >= MIN_REPEATED_EDGE_COMPONENTS;
    }

    let protected_left = regions.iter().any(|region| {
        region.left < strip_width && region.right > strip_width
    });
    let protected_right = regions.iter().any(|region| {
        region.left < width - strip_width && region.right > width - strip_width
    });
    if (repeated[0] && !protected_left) || (repeated[1] && !protected_right) {
        for row in foreground.chunks_exact_mut(width) {
            if repeated[0] && !protected_left {
                row[..strip_width].fill(false);
            }
            if repeated[1] && !protected_right {
                row[width - strip_width..].fill(false);
            }
        }
    }
}

fn detect_objects(
    lines: &[DetectedLine],
    regions: &[TableRegion],
    foreground: &[bool],
    width: usize,
) -> Vec<DetectedObject> {
    let mut claimed = vec![false; lines.len()];
    let mut objects = Vec::new();
    for region in regions {
        let members: Vec<DetectedLine> = lines
            .iter()
            .copied()
            .enumerate()
            .filter_map(|(index, line)| {
                let center_x = (line.left + line.right) / 2;
                let center_y = (line.top + line.bottom) / 2;
                if region.left <= center_x
                    && center_x <= region.right
                    && region.top <= center_y
                    && center_y <= region.bottom
                {
                    claimed[index] = true;
                    Some(line)
                } else {
                    None
                }
            })
            .collect();
        if !members.is_empty() {
            objects.push(DetectedObject {
                kind: OBJECT_TABLE,
                lines: members,
            });
        }
    }

    let remaining: Vec<DetectedLine> = lines
        .iter()
        .copied()
        .enumerate()
        .filter_map(|(index, line)| (!claimed[index]).then_some(line))
        .collect();
    if !remaining.is_empty() {
        let mut heights: Vec<usize> = remaining
            .iter()
            .map(|line| line.bottom - line.top)
            .collect();
        heights.sort_unstable();
        let typical_height = heights[(heights.len() - 1) / 2].max(1);
        let object_gap = typical_height.max(8);
        let mut groups: Vec<Vec<DetectedLine>> = Vec::new();
        for line in remaining {
            let begins_object = groups.last().and_then(|group| group.last()).is_some_and(|previous| {
                line.top.saturating_sub(previous.bottom) > object_gap
            }) || groups.is_empty();
            if begins_object {
                groups.push(Vec::new());
            }
            groups.last_mut().expect("group exists").push(line);
        }
        objects.extend(groups.into_iter().map(|group| {
            let marker_lines = group
                .iter()
                .copied()
                .filter(|line| line_has_list_marker(*line, foreground, width))
                .count();
            let group_height = group.last().expect("group is non-empty").bottom
                - group.first().expect("group is non-empty").top;
            DetectedObject {
                kind: if marker_lines >= 2 && group_height >= typical_height * 3 {
                    OBJECT_LIST
                } else {
                    OBJECT_PARAGRAPH
                },
                lines: group,
            }
        }));
    }
    objects.sort_by_key(|object| {
        let first = object.lines.first().expect("object is non-empty");
        (first.top, first.left)
    });
    objects
}

fn plan_jobs(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> Vec<OcrJob> {
    let regions = table_regions(pixels, width, height, stride, channels);
    let mut foreground = local_contrast_mask(pixels, width, height, stride, channels);
    remove_repeated_edge_foreground(&mut foreground, &regions, width, height);
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
    let mut merged_lines: Vec<(usize, usize)> = Vec::with_capacity(bands.len());
    for (top, bottom) in bands {
        if let Some(previous) = merged_lines.last_mut()
            && top.saturating_sub(previous.1) <= glyph_gap
        {
            previous.1 = bottom;
        } else {
            merged_lines.push((top, bottom));
        }
    }
    let mut lines: Vec<DetectedLine> = merged_lines
        .iter()
        .filter_map(|(top, bottom)| detected_line(&foreground, width, *top, *bottom))
        .collect();
    let minimum_line_ink = (width / 100).max(12);
    lines.retain(|line| {
        let inside_table = line_in_table_region(*line, &regions);
        let line_width = line.right.saturating_sub(line.left);
        let line_height = line.bottom.saturating_sub(line.top);
        let isolated_micro_component = line_width <= (width / 100).max(12)
            && line_height <= (height / 200).max(12);
        inside_table
            || (!isolated_micro_component
                && line_ink_count(*line, &foreground, width) >= minimum_line_ink)
    });
    remove_page_edge_artifacts(&mut lines, &regions, &foreground, width, height);
    if lines.is_empty() {
        return Vec::new();
    }
    let x_padding = (width / 800).clamp(2, 12);
    let y_padding = (height / 1200).clamp(1, 6);
    let objects = detect_objects(&lines, &regions, &foreground, width);

    const MAX_CONTEXT_LINES: usize = 16;
    let estimated_jobs = objects
        .iter()
        .map(|object| object.lines.len().div_ceil(MAX_CONTEXT_LINES))
        .sum::<usize>();
    let mut jobs = Vec::with_capacity(estimated_jobs.min(MAX_JOBS));
    let mut source_row = 0_u32;
    for (object_id, object) in objects.iter().enumerate() {
        for chunk in object.lines.chunks(MAX_CONTEXT_LINES) {
            let band_top = chunk[0].top;
            let band_bottom = chunk[chunk.len() - 1].bottom;
            let left = chunk.iter().map(|line| line.left).min().unwrap_or(width);
            let right = chunk.iter().map(|line| line.right).max().unwrap_or(0);
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
                object_kind: object.kind,
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
        10 => job.object_kind as i32,
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
        assert!(jobs[0].rect.right >= 70, "planned jobs: {jobs:?}");
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
