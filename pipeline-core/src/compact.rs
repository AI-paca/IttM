use std::collections::BTreeMap;

use crate::blocks::BlockPlan;
use crate::geometry::GeometryAnalysis;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CompactionPlacement {
    pub unit_index: usize,
    pub segment_indexes: Vec<usize>,
    pub source_bbox: [usize; 4],
    pub crop_bbox: [usize; 4],
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CompactedBlock {
    pub width: usize,
    pub height: usize,
    pub pixels: Vec<u8>,
    pub placements: Vec<CompactionPlacement>,
    pub omitted_unit_indexes: Vec<usize>,
    pub occupied_pixels_before: usize,
    pub occupied_pixels_after: usize,
    pub packed_canvas_pixels: usize,
}

#[derive(Clone, Debug)]
struct Tile {
    unit_index: usize,
    segment_indexes: Vec<usize>,
    source_bbox: [usize; 4],
    width: usize,
    height: usize,
    pixels: Vec<u8>,
}

fn bbox_union(values: impl IntoIterator<Item = [usize; 4]>) -> Option<[usize; 4]> {
    let mut values = values.into_iter();
    let mut result = values.next()?;
    for value in values {
        result[0] = result[0].min(value[0]);
        result[1] = result[1].min(value[1]);
        result[2] = result[2].max(value[2]);
        result[3] = result[3].max(value[3]);
    }
    Some(result)
}

fn luminance(pixel: &[u8]) -> u8 {
    (f32::from(pixel[0]) * 0.299 + f32::from(pixel[1]) * 0.587 + f32::from(pixel[2]) * 0.114)
        .round_ties_even()
        .clamp(0.0, 255.0) as u8
}

fn content_ink_mask(
    pixels: &[u8],
    width: usize,
    height: usize,
    unit_mask: &[bool],
) -> Option<(Vec<bool>, bool)> {
    if pixels.len() != width.checked_mul(height)?.checked_mul(3)?
        || unit_mask.len() != width.checked_mul(height)?
    {
        return None;
    }
    let luminance_values: Vec<u8> = pixels.chunks_exact(3).map(luminance).collect();
    let mut unit_left = width;
    let mut unit_top = height;
    let mut unit_right = 0;
    let mut unit_bottom = 0;
    for (index, selected) in unit_mask.iter().copied().enumerate() {
        if selected {
            let x = index % width;
            let y = index / width;
            unit_left = unit_left.min(x);
            unit_top = unit_top.min(y);
            unit_right = unit_right.max(x + 1);
            unit_bottom = unit_bottom.max(y + 1);
        }
    }
    if unit_left >= unit_right || unit_top >= unit_bottom {
        return Some((vec![false; unit_mask.len()], true));
    }
    let sample_left = unit_left.saturating_sub(4);
    let sample_top = unit_top.saturating_sub(4);
    let sample_right = (unit_right + 4).min(width);
    let sample_bottom = (unit_bottom + 4).min(height);
    let sample_size = (sample_right - sample_left) * (sample_bottom - sample_top);
    let mut sample_selected = 0_usize;
    let mut surrounding_values = Vec::<u8>::new();
    for y in sample_top..sample_bottom {
        for x in sample_left..sample_right {
            let index = y * width + x;
            if unit_mask[index] {
                sample_selected += 1;
            } else {
                surrounding_values.push(luminance_values[index]);
            }
        }
    }
    let sample_density = sample_selected as f64 / sample_size.max(1) as f64;
    let inside_values: Vec<u8> = unit_mask
        .iter()
        .enumerate()
        .filter_map(|(index, selected)| selected.then_some(luminance_values[index]))
        .collect();
    let values = if sample_density >= 0.45 {
        &inside_values
    } else if surrounding_values.len() >= 16 {
        &surrounding_values
    } else {
        &inside_values
    };
    if values.is_empty() {
        return Some((vec![false; unit_mask.len()], true));
    }
    let mut histogram = [0_usize; 256];
    for value in values {
        histogram[*value as usize] += 1;
    }
    let background = histogram
        .iter()
        .enumerate()
        .max_by_key(|(value, count)| (**count, std::cmp::Reverse(*value)))
        .map(|(value, _)| value)?;
    let dark_on_light = background >= 128;
    let mut ink: Vec<bool> = unit_mask
        .iter()
        .enumerate()
        .map(|(index, selected)| {
            *selected
                && if dark_on_light {
                    luminance_values[index] <= background.saturating_sub(12) as u8
                } else {
                    luminance_values[index] >= (background + 12).min(255) as u8
                }
        })
        .collect();
    let minimum_horizontal_rule = 32.max((width * 65).div_ceil(100));
    let minimum_vertical_rule = 32.max((height * 65).div_ceil(100));
    let horizontal_rules: Vec<bool> = (0..height)
        .map(|y| {
            ink[y * width..(y + 1) * width]
                .iter()
                .filter(|value| **value)
                .count()
                >= minimum_horizontal_rule
        })
        .collect();
    let vertical_rules: Vec<bool> = (0..width)
        .map(|x| (0..height).filter(|y| ink[*y * width + x]).count() >= minimum_vertical_rule)
        .collect();
    for y in 0..height {
        for x in 0..width {
            if horizontal_rules[y] || vertical_rules[x] {
                ink[y * width + x] = false;
            }
        }
    }
    Some((ink, dark_on_light))
}

fn dilate_mask(mask: &[bool], width: usize, height: usize) -> Vec<bool> {
    let mut output = vec![false; mask.len()];
    for y in 0..height {
        for x in 0..width {
            output[y * width + x] =
                (y.saturating_sub(1)..=(y + 1).min(height - 1)).any(|source_y| {
                    (x.saturating_sub(1)..=(x + 1).min(width - 1))
                        .any(|source_x| mask[source_y * width + source_x])
                });
        }
    }
    output
}

fn retained_indexes(active: &[bool], maximum_gap: usize) -> Vec<usize> {
    let occupied: Vec<usize> = active
        .iter()
        .enumerate()
        .filter_map(|(index, value)| (*value).then_some(index))
        .collect();
    let mut keep = vec![true; active.len()];
    for pair in occupied.windows(2) {
        let blank = pair[1] - pair[0] - 1;
        if blank <= maximum_gap {
            continue;
        }
        let left_context = maximum_gap / 2;
        let right_context = maximum_gap - left_context;
        keep[pair[0] + 1 + left_context..pair[1] - right_context].fill(false);
    }
    keep.iter()
        .enumerate()
        .filter_map(|(index, value)| (*value).then_some(index))
        .collect()
}

fn compact_internal_whitespace(
    pixels: &[u8],
    ink: &[bool],
    width: usize,
    height: usize,
) -> Option<(Vec<u8>, usize, usize)> {
    if !ink.iter().any(|value| *value) {
        return Some((pixels.to_vec(), width, height));
    }
    let active_rows: Vec<bool> = (0..height)
        .map(|y| ink[y * width..(y + 1) * width].iter().any(|value| *value))
        .collect();
    let active_columns: Vec<bool> = (0..width)
        .map(|x| (0..height).any(|y| ink[y * width + x]))
        .collect();
    let column_gap = 8.max((height * 3).div_ceil(4)).min(32);
    let row_gap = 6.max((width * 2).div_ceil(25)).min(24);
    let rows = retained_indexes(&active_rows, row_gap);
    let columns = retained_indexes(&active_columns, column_gap);
    let mut output = Vec::with_capacity(rows.len() * columns.len() * 3);
    for y in &rows {
        for x in &columns {
            let source = (*y * width + *x) * 3;
            output.extend_from_slice(&pixels[source..source + 3]);
        }
    }
    Some((output, columns.len(), rows.len()))
}

fn ceil_sqrt(value: usize) -> usize {
    let mut root = (value as f64).sqrt() as usize;
    while root.saturating_mul(root) < value {
        root += 1;
    }
    while root > 0 && (root - 1).saturating_mul(root - 1) >= value {
        root -= 1;
    }
    root
}

fn lanczos(value: f64) -> f64 {
    if value == 0.0 {
        return 1.0;
    }
    if value.abs() >= 3.0 {
        return 0.0;
    }
    let pi_value = std::f64::consts::PI * value;
    (pi_value.sin() / pi_value) * ((pi_value / 3.0).sin() / (pi_value / 3.0))
}

pub(crate) fn resize_lanczos(pixels: &[u8], width: usize, height: usize, scale: usize) -> Vec<u8> {
    if scale == 1 {
        return pixels.to_vec();
    }
    const PRECISION_BITS: i32 = 22;
    const PRECISION_ROUNDING: i64 = 1_i64 << (PRECISION_BITS - 1);
    let output_width = width * scale;
    let output_height = height * scale;
    let coefficients = |output_length: usize, input_length: usize| {
        (0..output_length)
            .map(|target| {
                let center = (target as f64 + 0.5) / scale as f64;
                let start = ((center - 3.0 + 0.5) as isize).max(0) as usize;
                let stop = ((center + 3.0 + 0.5) as isize)
                    .max(0)
                    .min(input_length as isize) as usize;
                let weights: Vec<f64> = (start..stop)
                    .map(|source| lanczos(source as f64 + 0.5 - center))
                    .collect();
                let total = weights.iter().sum::<f64>();
                let weights = weights
                    .into_iter()
                    .map(|weight| {
                        (weight / total * f64::from(1_i32 << PRECISION_BITS)).round() as i32
                    })
                    .collect::<Vec<_>>();
                (start, weights)
            })
            .collect::<Vec<_>>()
    };
    let horizontal_coefficients = coefficients(output_width, width);
    let vertical_coefficients = coefficients(output_height, height);
    let mut horizontal = vec![0_u8; output_width * height * 3];
    for y in 0..height {
        for target_x in 0..output_width {
            let (start, weights) = &horizontal_coefficients[target_x];
            let mut values = [PRECISION_ROUNDING; 3];
            for (offset, weight) in weights.iter().enumerate() {
                let source = (y * width + *start + offset) * 3;
                for channel in 0..3 {
                    values[channel] += i64::from(pixels[source + channel]) * i64::from(*weight);
                }
            }
            let target = (y * output_width + target_x) * 3;
            for channel in 0..3 {
                horizontal[target + channel] =
                    (values[channel] >> PRECISION_BITS).clamp(0, 255) as u8;
            }
        }
    }
    let mut output = vec![0_u8; output_width * output_height * 3];
    for target_y in 0..output_height {
        let (start, weights) = &vertical_coefficients[target_y];
        for x in 0..output_width {
            let mut values = [PRECISION_ROUNDING; 3];
            for (offset, weight) in weights.iter().enumerate() {
                let source = ((*start + offset) * output_width + x) * 3;
                for channel in 0..3 {
                    values[channel] += i64::from(horizontal[source + channel]) * i64::from(*weight);
                }
            }
            let target = (target_y * output_width + x) * 3;
            for channel in 0..3 {
                output[target + channel] = (values[channel] >> PRECISION_BITS).clamp(0, 255) as u8;
            }
        }
    }
    output
}

fn canonical_packed_shape(block: &crate::blocks::RecognitionBlock) -> Option<[usize; 2]> {
    let shape = block.logical_scope_shape?;
    if shape[0] == 0 || shape[1] == 0 || shape[0] > 16 || shape[1] > 16 {
        return None;
    }
    block
        .logical_segment_spans
        .iter()
        .enumerate()
        .all(|(position, span)| {
            let row = position / shape[1];
            let column = position % shape[1];
            *span == [row, row + 1, column, column + 1]
        })
        .then_some(shape)
}

fn pack_tiles(tiles: &[Tile], canonical_shape: Option<[usize; 2]>) -> Option<CompactedBlock> {
    let gap = 8_usize;
    let (atlas_width, atlas_height, positions) = if canonical_shape.is_some() {
        if tiles.len() > 256 {
            return None;
        }
        let columns = ceil_sqrt(tiles.len()).clamp(1, 16);
        let rows = tiles.len().div_ceil(columns);
        if rows > 16 {
            return None;
        }
        let mut column_widths = vec![0_usize; columns];
        let mut row_heights = vec![0_usize; rows];
        for (index, tile) in tiles.iter().enumerate() {
            column_widths[index % columns] = column_widths[index % columns].max(tile.width);
            row_heights[index / columns] = row_heights[index / columns].max(tile.height);
        }
        let mut column_lefts = Vec::with_capacity(columns);
        let mut left = 0_usize;
        for width in &column_widths {
            column_lefts.push(left);
            left += width + gap;
        }
        let atlas_width = left.saturating_sub(gap);
        let mut row_tops = Vec::with_capacity(rows);
        let mut top = 0_usize;
        for height in &row_heights {
            row_tops.push(top);
            top += height + gap;
        }
        let atlas_height = top.saturating_sub(gap);
        let positions = tiles
            .iter()
            .enumerate()
            .map(|(index, tile)| {
                let row = index / columns;
                let column = index % columns;
                (
                    index,
                    column_lefts[column] + (column_widths[column] - tile.width) / 2,
                    row_tops[row] + (row_heights[row] - tile.height) / 2,
                )
            })
            .collect();
        (atlas_width, atlas_height, positions)
    } else {
        let total_tile_pixels = tiles
            .iter()
            .map(|tile| tile.width * tile.height)
            .sum::<usize>();
        let target_width = tiles
            .iter()
            .map(|tile| tile.width)
            .max()?
            .max(ceil_sqrt(total_tile_pixels));
        let mut rows = Vec::<Vec<usize>>::new();
        let mut current = Vec::<usize>::new();
        let mut current_width = 0_usize;
        for (index, tile) in tiles.iter().enumerate() {
            let projected = if current.is_empty() {
                tile.width
            } else {
                current_width + gap + tile.width
            };
            if !current.is_empty() && projected > target_width {
                rows.push(std::mem::take(&mut current));
                current_width = 0;
            }
            current.push(index);
            current_width = if current.len() > 1 {
                current_width + gap + tile.width
            } else {
                tile.width
            };
        }
        if !current.is_empty() {
            rows.push(current);
        }
        let row_heights: Vec<usize> = rows
            .iter()
            .map(|row| {
                row.iter()
                    .map(|index| tiles[*index].height)
                    .max()
                    .unwrap_or(0)
            })
            .collect();
        let row_widths: Vec<usize> = rows
            .iter()
            .map(|row| {
                row.iter().map(|index| tiles[*index].width).sum::<usize>()
                    + gap * row.len().saturating_sub(1)
            })
            .collect();
        let atlas_width = *row_widths.iter().max()?;
        let atlas_height = row_heights.iter().sum::<usize>() + gap * rows.len().saturating_sub(1);
        let mut positions = Vec::<(usize, usize, usize)>::new();
        let mut top = 0_usize;
        for (row, row_height) in rows.iter().zip(&row_heights) {
            let mut left = 0_usize;
            for index in row {
                positions.push((*index, left, top));
                left += tiles[*index].width + gap;
            }
            top += row_height + gap;
        }
        (atlas_width, atlas_height, positions)
    };
    let mut atlas = vec![255_u8; atlas_width * atlas_height * 3];
    for (index, left, top) in &positions {
        let tile = &tiles[*index];
        for y in 0..tile.height {
            let source = y * tile.width * 3;
            let target = ((*top + y) * atlas_width + *left) * 3;
            atlas[target..target + tile.width * 3]
                .copy_from_slice(&tile.pixels[source..source + tile.width * 3]);
        }
    }
    let mut line_heights = Vec::<usize>::new();
    for tile in tiles {
        let ink: Vec<bool> = tile
            .pixels
            .chunks_exact(3)
            .map(|pixel| pixel.iter().any(|value| *value < 248))
            .collect();
        let minimum_rule = 2.max((tile.height * 3).div_ceil(4));
        let non_rule_columns: Vec<usize> = (0..tile.width)
            .filter(|x| {
                (0..tile.height)
                    .filter(|y| ink[*y * tile.width + *x])
                    .count()
                    < minimum_rule
            })
            .collect();
        let occupied_rows: Vec<usize> = (0..tile.height)
            .filter(|y| {
                if non_rule_columns.is_empty() {
                    (0..tile.width).any(|x| ink[*y * tile.width + x])
                } else {
                    non_rule_columns.iter().any(|x| ink[*y * tile.width + *x])
                }
            })
            .collect();
        let Some(mut run_start) = occupied_rows.first().copied() else {
            continue;
        };
        let mut previous = run_start;
        for row in occupied_rows.into_iter().skip(1) {
            if row > previous + 1 {
                let run_height = previous - run_start + 1;
                if run_height >= 2 {
                    line_heights.push(run_height);
                }
                run_start = row;
            }
            previous = row;
        }
        let run_height = previous - run_start + 1;
        if run_height >= 2 {
            line_heights.push(run_height);
        }
    }
    line_heights.sort_unstable();
    let median_text_height = line_heights
        .get(line_heights.len() / 2)
        .copied()
        .unwrap_or(24);
    let requested_scale = 24_usize.div_ceil(median_text_height.max(1)).clamp(1, 4);
    let pixel_limited_scale = ((16_000_000 / (atlas_width * atlas_height).max(1)) as f64)
        .sqrt()
        .floor() as usize;
    let scale = requested_scale.min(pixel_limited_scale.max(1));
    let pixels = resize_lanczos(&atlas, atlas_width, atlas_height, scale);
    let placements: Vec<CompactionPlacement> = positions
        .into_iter()
        .map(|(index, left, top)| {
            let tile = &tiles[index];
            CompactionPlacement {
                unit_index: tile.unit_index,
                segment_indexes: tile.segment_indexes.clone(),
                source_bbox: tile.source_bbox,
                crop_bbox: [
                    left * scale,
                    top * scale,
                    (left + tile.width) * scale,
                    (top + tile.height) * scale,
                ],
            }
        })
        .collect();
    let occupied_before = tiles.iter().map(|tile| tile.width * tile.height).sum();
    let occupied_after = placements
        .iter()
        .map(|placement| {
            (placement.crop_bbox[2] - placement.crop_bbox[0])
                * (placement.crop_bbox[3] - placement.crop_bbox[1])
        })
        .sum();
    Some(CompactedBlock {
        width: atlas_width * scale,
        height: atlas_height * scale,
        pixels,
        placements,
        omitted_unit_indexes: Vec::new(),
        occupied_pixels_before: occupied_before,
        occupied_pixels_after: occupied_after,
        packed_canvas_pixels: atlas_width * atlas_height * scale * scale,
    })
}

fn crop_rgb(page: &[u8], page_width: usize, bbox: [usize; 4]) -> Vec<u8> {
    let width = bbox[2] - bbox[0];
    let mut output = Vec::with_capacity(width * (bbox[3] - bbox[1]) * 3);
    for y in bbox[1]..bbox[3] {
        let start = (y * page_width + bbox[0]) * 3;
        output.extend_from_slice(&page[start..start + width * 3]);
    }
    output
}

pub fn compact_blocks(
    analysis: &GeometryAnalysis,
    plan: &BlockPlan,
) -> Option<Vec<CompactedBlock>> {
    let width = analysis.foreground.width;
    let height = analysis.foreground.height;
    let page = &analysis.foreground.pixels;
    if page.len() != width.checked_mul(height)?.checked_mul(3)?
        || analysis.materialized_segments.ownership.len() != width.checked_mul(height)?
    {
        return None;
    }
    let segments = &analysis.materialized_segments.segments;
    let mut unit_by_segment = vec![None; segments.len()];
    for (unit_index, unit) in plan.membership_units.iter().enumerate() {
        for segment in &unit.segment_indexes {
            unit_by_segment[*segment] = Some(unit_index);
        }
    }
    let mut tile_cache = BTreeMap::<Vec<usize>, Option<Tile>>::new();
    let mut outputs = Vec::with_capacity(plan.blocks.len());
    for (block_index, block) in plan.blocks.iter().enumerate() {
        if block.logical_segment_spans.len() != block.segment_indexes.len() {
            return None;
        }
        let mut groups = Vec::<([usize; 4], Vec<usize>)>::new();
        for (segment, key) in block
            .segment_indexes
            .iter()
            .zip(&block.logical_segment_spans)
        {
            if let Some((_, members)) = groups.iter_mut().find(|(value, _)| *value == *key) {
                members.push(*segment);
            } else {
                groups.push((*key, vec![*segment]));
            }
        }
        let mut tiles = Vec::<Tile>::new();
        let mut omitted = Vec::<usize>::new();
        for (_, group) in groups {
            let unit_index = unit_by_segment[*group.first()?]?;
            if let Some(cached) = tile_cache.get(&group) {
                if let Some(tile) = cached {
                    tiles.push(tile.clone());
                } else {
                    omitted.push(unit_index);
                }
                continue;
            }
            let member_bbox = bbox_union(group.iter().map(|index| segments[*index].bbox))?;
            let analysis_bbox = [
                member_bbox[0].saturating_sub(4),
                member_bbox[1].saturating_sub(4),
                (member_bbox[2] + 4).min(width),
                (member_bbox[3] + 4).min(height),
            ];
            let unit_width = analysis_bbox[2] - analysis_bbox[0];
            let unit_height = analysis_bbox[3] - analysis_bbox[1];
            let unit_pixels = crop_rgb(page, width, analysis_bbox);
            let mut unit_mask = vec![false; unit_width * unit_height];
            for y in analysis_bbox[1]..analysis_bbox[3] {
                for x in analysis_bbox[0]..analysis_bbox[2] {
                    let owner = analysis.materialized_segments.ownership[y * width + x];
                    unit_mask[(y - analysis_bbox[1]) * unit_width + x - analysis_bbox[0]] =
                        owner >= 0 && group.contains(&(owner as usize));
                }
            }
            let (ink, dark_on_light) =
                content_ink_mask(&unit_pixels, unit_width, unit_height, &unit_mask)?;
            let ink_indexes: Vec<usize> = ink
                .iter()
                .enumerate()
                .filter_map(|(index, value)| (*value).then_some(index))
                .collect();
            if ink_indexes.is_empty() {
                tile_cache.insert(group.clone(), None);
                omitted.push(unit_index);
                continue;
            }
            let local_left = ink_indexes
                .iter()
                .map(|index| index % unit_width)
                .min()?
                .saturating_sub(2);
            let local_top = ink_indexes
                .iter()
                .map(|index| index / unit_width)
                .min()?
                .saturating_sub(2);
            let local_right =
                (ink_indexes.iter().map(|index| index % unit_width).max()? + 3).min(unit_width);
            let local_bottom =
                (ink_indexes.iter().map(|index| index / unit_width).max()? + 3).min(unit_height);
            let tile_width = local_right - local_left;
            let tile_height = local_bottom - local_top;
            let mut tile_pixels = Vec::with_capacity(tile_width * tile_height * 3);
            let mut local_ink = Vec::with_capacity(tile_width * tile_height);
            for y in local_top..local_bottom {
                for x in local_left..local_right {
                    let source = (y * unit_width + x) * 3;
                    tile_pixels.extend_from_slice(&unit_pixels[source..source + 3]);
                    local_ink.push(ink[y * unit_width + x]);
                }
            }
            let retained = dilate_mask(&local_ink, tile_width, tile_height);
            if dark_on_light {
                for (index, keep) in retained.iter().enumerate() {
                    if !keep {
                        tile_pixels[index * 3..index * 3 + 3].fill(255);
                    }
                }
            } else {
                for (index, keep) in retained.iter().enumerate() {
                    let value = if *keep {
                        255 - luminance(&tile_pixels[index * 3..index * 3 + 3])
                    } else {
                        255
                    };
                    tile_pixels[index * 3..index * 3 + 3].fill(value);
                }
            }
            let (tile_pixels, compact_width, compact_height) =
                compact_internal_whitespace(&tile_pixels, &local_ink, tile_width, tile_height)?;
            let tile = Tile {
                unit_index,
                segment_indexes: group.clone(),
                source_bbox: [
                    analysis_bbox[0] + local_left,
                    analysis_bbox[1] + local_top,
                    analysis_bbox[0] + local_right,
                    analysis_bbox[1] + local_bottom,
                ],
                width: compact_width,
                height: compact_height,
                pixels: tile_pixels,
            };
            tile_cache.insert(group, Some(tile.clone()));
            tiles.push(tile);
        }
        let used_fallback = tiles.is_empty();
        let mut compacted = if used_fallback {
            let bbox = block.bbox;
            let crop_width = bbox[2] - bbox[0];
            let crop_height = bbox[3] - bbox[1];
            let mut pixels = crop_rgb(page, width, bbox);
            for y in bbox[1]..bbox[3] {
                for x in bbox[0]..bbox[2] {
                    let owner = analysis.materialized_segments.ownership[y * width + x];
                    if owner >= 0 && !block.segment_indexes.contains(&(owner as usize)) {
                        let target = ((y - bbox[1]) * crop_width + x - bbox[0]) * 3;
                        pixels[target..target + 3].fill(255);
                    }
                }
            }
            let block_units: Vec<usize> = plan
                .membership_units
                .iter()
                .enumerate()
                .filter_map(|(index, unit)| {
                    (unit.block_indexes.contains(&block_index)
                        && unit
                            .segment_indexes
                            .iter()
                            .all(|value| block.segment_indexes.contains(value)))
                    .then_some(index)
                })
                .collect();
            let placements = if block_units.len() == 1 {
                let unit = &plan.membership_units[block_units[0]];
                vec![CompactionPlacement {
                    unit_index: block_units[0],
                    segment_indexes: unit.segment_indexes.clone(),
                    source_bbox: bbox,
                    crop_bbox: [0, 0, crop_width, crop_height],
                }]
            } else {
                Vec::new()
            };
            CompactedBlock {
                width: crop_width,
                height: crop_height,
                pixels,
                placements,
                omitted_unit_indexes: Vec::new(),
                occupied_pixels_before: crop_width * crop_height,
                occupied_pixels_after: crop_width * crop_height,
                packed_canvas_pixels: crop_width * crop_height,
            }
        } else {
            pack_tiles(&tiles, canonical_packed_shape(block))?
        };
        let mut unique_omitted = Vec::new();
        for unit in omitted {
            if !unique_omitted.contains(&unit) {
                unique_omitted.push(unit);
            }
        }
        if !used_fallback {
            compacted.omitted_unit_indexes = unique_omitted;
        }
        outputs.push(compacted);
    }
    Some(outputs)
}
