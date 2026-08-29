use std::sync::Arc;

use crate::local_structure::{
    LocalLine, LocalNetwork, LocalStructureConfig, detect_local_structures_with_config,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ForegroundGeometry {
    pub width: usize,
    pub height: usize,
    pub pixels: Vec<u8>,
    pub stride: usize,
    pub background: [u8; 3],
    pub physical_foreground: Vec<bool>,
    pub foreground: Vec<bool>,
    pub rule_evidence: Vec<bool>,
    pub mode: String,
    pub correction_milli_degrees: i32,
    pub region_operation: String,
    pub region_confidence_ppm: i32,
    pub region_source_angle_milli_degrees: i32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ConnectedComponent {
    pub bbox: [usize; 4],
    pub pixels: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ConnectedComponentRun {
    pub component_index: usize,
    pub row: usize,
    pub start: usize,
    pub stop: usize,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RegularRowGrid {
    pub pitch: usize,
    pub phase: usize,
    pub row_count: usize,
    pub score: f64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SeparatorChoice {
    pub rows: bool,
    pub bbox: [usize; 4],
    pub rule_seam: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RecoveredRuleDraft {
    pub summary: RuleDraftSummary,
    pub claim_full_bbox: bool,
    pub partition_evidence: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuleCandidateDraft {
    pub summary: RuleDraftSummary,
    pub candidate_mask: Arc<[u8]>,
    pub claim_full_bbox: bool,
    pub partition_evidence: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ComponentRowBoundary {
    pub coordinate: usize,
    pub upper_indexes: Vec<usize>,
    pub lower_indexes: Vec<usize>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ComponentColumnBoundary {
    pub separator: [usize; 4],
    pub left_indexes: Vec<usize>,
    pub right_indexes: Vec<usize>,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct AffineBoxTransform {
    pub original_size: [usize; 2],
    pub inverse: [f64; 9],
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MaterializedSegment {
    pub bbox: [usize; 4],
    pub source_bbox: [usize; 4],
    pub ink_pixels: usize,
    pub row_index: usize,
    pub leaf_index: usize,
    pub component_ids: Vec<usize>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MaterializedSegments {
    pub segments: Vec<MaterializedSegment>,
    pub ownership: Vec<isize>,
    pub leaf_segment_indexes: Vec<Option<usize>>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MaterializedNode {
    pub source_index: usize,
    pub segment_indexes: Vec<usize>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SparseAxisInterval {
    pub start: usize,
    pub end: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub struct MaterializedSparseCell {
    pub row: usize,
    pub column: usize,
    pub segment_index: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MaterializedSegmentSpan {
    pub segment_index: usize,
    pub row_start: usize,
    pub row_stop: usize,
    pub column_start: usize,
    pub column_stop: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MaterializedSparseMatrix {
    pub rows: Vec<SparseAxisInterval>,
    pub columns: Vec<SparseAxisInterval>,
    pub cells: Vec<MaterializedSparseCell>,
    pub spans: Vec<MaterializedSegmentSpan>,
    pub horizontal_rule_rows: Vec<usize>,
    pub vertical_rule_columns: Vec<usize>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct GeometryAnalysis {
    pub foreground: ForegroundGeometry,
    pub ownership_foreground: Vec<u8>,
    pub rules: Vec<MaterializedRule>,
    pub rule_mask: Vec<u8>,
    pub components: Vec<ConnectedComponent>,
    pub component_runs: Vec<ConnectedComponentRun>,
    pub partition_nodes: Vec<PartitionNode>,
    pub materialized_segments: MaterializedSegments,
    pub materialized_nodes: Vec<MaterializedNode>,
    pub sparse_matrix: MaterializedSparseMatrix,
    pub recovered_rule_count: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PartitionStopReason {
    Atomic,
    Empty,
    Limit,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PartitionNode {
    pub bbox: [usize; 4],
    pub depth: usize,
    pub parent_index: Option<usize>,
    pub rows: Option<bool>,
    pub child_indexes: [Option<usize>; 2],
    pub separator: Option<[usize; 4]>,
    pub split_coordinate: Option<usize>,
    pub stop_reason: Option<PartitionStopReason>,
    pub rule_partition: bool,
    pub row_rule_top: bool,
    pub row_rule_bottom: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PartitionConfig {
    pub max_nodes: usize,
    pub max_depth: usize,
    pub min_safe_gap: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RuleDraftSummary {
    pub horizontal: bool,
    pub bbox: [usize; 4],
}

#[derive(Clone, Debug, PartialEq)]
struct LocalLineBand {
    coordinate: usize,
    coverage: f64,
    members: Vec<LocalLine>,
}

fn local_line_bands(
    network: &LocalNetwork,
    horizontal: bool,
    tolerance: usize,
) -> Vec<LocalLineBand> {
    let mut source: Vec<LocalLine> = network
        .lines
        .iter()
        .copied()
        .filter(|line| line.horizontal == horizontal)
        .collect();
    let center = |line: &LocalLine| {
        if horizontal {
            (line.bbox[1] + line.bbox[3]) as f64 / 2.0
        } else {
            (line.bbox[0] + line.bbox[2]) as f64 / 2.0
        }
    };
    source.sort_by(|first, second| center(first).total_cmp(&center(second)));
    let mut groups = Vec::<Vec<LocalLine>>::new();
    let mut centers = Vec::<f64>::new();
    for line in source {
        let line_center = center(&line);
        if !groups.is_empty()
            && (line_center - centers[centers.len() - 1]).abs() <= tolerance as f64
        {
            groups.last_mut().expect("existing group").push(line);
            centers.last_mut().expect("existing center").clone_from(
                &(groups
                    .last()
                    .expect("existing group")
                    .iter()
                    .map(center)
                    .sum::<f64>()
                    / groups.last().expect("existing group").len() as f64),
            );
        } else {
            groups.push(vec![line]);
            centers.push(line_center);
        }
    }
    let span_start = if horizontal {
        network.bbox[0]
    } else {
        network.bbox[1]
    };
    let span_stop = if horizontal {
        network.bbox[2]
    } else {
        network.bbox[3]
    };
    let span = (span_stop - span_start).max(1);
    centers
        .into_iter()
        .zip(groups)
        .map(|(center, members)| {
            let mut intervals: Vec<(usize, usize)> = members
                .iter()
                .map(|line| {
                    if horizontal {
                        (span_start.max(line.bbox[0]), span_stop.min(line.bbox[2]))
                    } else {
                        (span_start.max(line.bbox[1]), span_stop.min(line.bbox[3]))
                    }
                })
                .collect();
            intervals.sort_unstable();
            let (mut current_start, mut current_stop) = intervals[0];
            let mut covered = 0;
            for (start, stop) in intervals.into_iter().skip(1) {
                if start <= current_stop + tolerance {
                    current_stop = current_stop.max(stop);
                } else {
                    covered += current_stop.saturating_sub(current_start);
                    current_start = start;
                    current_stop = stop;
                }
            }
            covered += current_stop.saturating_sub(current_start);
            LocalLineBand {
                coordinate: center.round_ties_even() as usize,
                coverage: covered as f64 / span as f64,
                members,
            }
        })
        .collect()
}

pub fn local_line_band_rows(rgb: &[u8], width: usize, height: usize) -> Option<Vec<[u32; 5]>> {
    let result = detect_local_structures_with_config(
        rgb,
        width,
        height,
        LocalStructureConfig {
            minimum_contrast: 8,
            minimum_length: 24,
            maximum_gap: 1,
            maximum_line_thickness: 4,
            junction_tolerance: 2,
        },
    )?;
    let mut rows = Vec::new();
    for (network_index, network) in result.networks.iter().enumerate() {
        for horizontal in [true, false] {
            for band in local_line_bands(network, horizontal, 4) {
                for member in band.members {
                    let line_index = result.lines.iter().position(|line| *line == member)?;
                    rows.push([
                        u32::try_from(network_index).ok()?,
                        u32::from(!horizontal),
                        u32::try_from(band.coordinate).ok()?,
                        (band.coverage * 1_000_000.0).round() as u32,
                        u32::try_from(line_index).ok()?,
                    ]);
                }
            }
        }
    }
    Some(rows)
}

#[derive(Clone, Copy, Debug)]
struct ForegroundRun {
    row: usize,
    start: usize,
    stop: usize,
    label: usize,
}

pub fn connected_components(
    mask: &[u8],
    width: usize,
    height: usize,
) -> Option<Vec<ConnectedComponent>> {
    let (components, _) = connected_components_with_runs(mask, width, height)?;
    Some(components)
}

pub fn connected_component_runs(
    mask: &[u8],
    width: usize,
    height: usize,
) -> Option<(Vec<ConnectedComponent>, Vec<ConnectedComponentRun>)> {
    connected_components_with_runs(mask, width, height)
}

fn connected_components_with_runs(
    mask: &[u8],
    width: usize,
    height: usize,
) -> Option<(Vec<ConnectedComponent>, Vec<ConnectedComponentRun>)> {
    if width == 0 || height == 0 || mask.len() != width.checked_mul(height)? {
        return None;
    }
    let mut parents = Vec::<usize>::new();
    let mut runs = Vec::<ForegroundRun>::new();
    let mut previous = Vec::<ForegroundRun>::new();
    for row in 0..height {
        let mut current = Vec::new();
        let mut previous_index = 0;
        let mut column = 0;
        while column < width {
            while column < width && mask[row * width + column] == 0 {
                column += 1;
            }
            if column == width {
                break;
            }
            let start = column;
            while column < width && mask[row * width + column] != 0 {
                column += 1;
            }
            let stop = column;
            let label = parents.len();
            parents.push(label);
            let run = ForegroundRun {
                row,
                start,
                stop,
                label,
            };
            while previous_index < previous.len() && previous[previous_index].stop < start {
                previous_index += 1;
            }
            let mut overlap_index = previous_index;
            while overlap_index < previous.len() && previous[overlap_index].start <= stop {
                union_component_labels(&mut parents, label, previous[overlap_index].label);
                overlap_index += 1;
            }
            current.push(run);
            runs.push(run);
        }
        previous = current;
    }
    let mut grouped = std::collections::BTreeMap::<usize, Vec<ForegroundRun>>::new();
    for run in runs {
        let root = find_component_label(&mut parents, run.label);
        grouped.entry(root).or_default().push(run);
    }
    let mut detailed: Vec<(ConnectedComponent, Vec<ForegroundRun>)> = grouped
        .into_values()
        .map(|component_runs| {
            (
                ConnectedComponent {
                    bbox: [
                        component_runs
                            .iter()
                            .map(|run| run.start)
                            .min()
                            .unwrap_or(0),
                        component_runs.iter().map(|run| run.row).min().unwrap_or(0),
                        component_runs.iter().map(|run| run.stop).max().unwrap_or(0),
                        component_runs
                            .iter()
                            .map(|run| run.row + 1)
                            .max()
                            .unwrap_or(0),
                    ],
                    pixels: component_runs.iter().map(|run| run.stop - run.start).sum(),
                },
                component_runs,
            )
        })
        .collect();
    detailed.sort_by_key(|(component, _)| {
        (
            component.bbox[1],
            component.bbox[0],
            component.bbox[3],
            component.bbox[2],
        )
    });
    let mut components = Vec::with_capacity(detailed.len());
    let mut component_runs = Vec::new();
    for (component_index, (component, runs)) in detailed.into_iter().enumerate() {
        components.push(component);
        component_runs.extend(runs.into_iter().map(|run| ConnectedComponentRun {
            component_index,
            row: run.row,
            start: run.start,
            stop: run.stop,
        }));
    }
    Some((components, component_runs))
}

fn linear_percentile(values: &[usize], percentile: f64) -> f64 {
    let mut ordered: Vec<usize> = values.to_vec();
    ordered.sort_unstable();
    if ordered.len() == 1 {
        return ordered[0] as f64;
    }
    let position = (ordered.len() - 1) as f64 * percentile / 100.0;
    let lower = position.floor() as usize;
    let upper = position.ceil() as usize;
    let fraction = position - lower as f64;
    ordered[lower] as f64 * (1.0 - fraction) + ordered[upper] as f64 * fraction
}

fn body_component_height(components: &[ConnectedComponent], percentile: f64) -> f64 {
    if components.is_empty() {
        return 0.0;
    }
    let body_candidates: Vec<usize> = components
        .iter()
        .filter(|component| component.pixels >= 4 && component.bbox[3] - component.bbox[1] >= 3)
        .map(|component| component.bbox[3] - component.bbox[1])
        .collect();
    let heights: Vec<usize> = if body_candidates.is_empty() {
        components
            .iter()
            .map(|component| component.bbox[3] - component.bbox[1])
            .collect()
    } else {
        body_candidates
    };
    let median = linear_percentile(&heights, 50.0);
    linear_percentile(&heights, percentile).min(2.0 * median)
}

pub fn estimate_regular_row_grid(
    mask: &[u8],
    width: usize,
    height: usize,
) -> Option<Option<RegularRowGrid>> {
    if width == 0 || height == 0 || mask.len() != width.checked_mul(height)? {
        return None;
    }
    let components = connected_components(mask, width, height)?;
    let projection: Vec<usize> = mask
        .chunks_exact(width)
        .map(|row| row.iter().filter(|value| **value != 0).count())
        .collect();
    let occupied_count = projection.iter().filter(|value| **value != 0).count();
    if occupied_count < 6 {
        return Some(None);
    }
    let content_top = projection
        .iter()
        .position(|value| *value != 0)
        .expect("occupied projection has a first row");
    let content_bottom = projection
        .iter()
        .rposition(|value| *value != 0)
        .expect("occupied projection has a final row")
        + 1;
    let content: Vec<f64> = projection[content_top..content_bottom]
        .iter()
        .map(|value| *value as f64)
        .collect();
    let maximum_pitch = 128_usize.min(content.len() / 2);
    if maximum_pitch < 3 {
        return Some(None);
    }

    let top_inset = content_top;
    let bottom_inset = height - content_bottom;
    let first_symmetric_phase = top_inset
        .saturating_sub(2)
        .max(bottom_inset.saturating_sub(2));
    let last_symmetric_phase = top_inset.min(bottom_inset);
    if first_symmetric_phase > last_symmetric_phase {
        return Some(None);
    }
    let mut page_fits = Vec::new();
    for pitch in 3..=maximum_pitch {
        let mut fit = None;
        let mut fit_count = 0;
        for phase in first_symmetric_phase..=last_symmetric_phase {
            let inner_height = height.saturating_sub(2 * phase);
            if inner_height == 0 || inner_height % pitch != 0 {
                continue;
            }
            let row_count = inner_height / pitch;
            if row_count >= 3 {
                fit = Some((row_count, phase));
                fit_count += 1;
            }
        }
        if fit_count == 1 {
            let (row_count, phase) = fit.expect("a unique fit has a value");
            page_fits.push((pitch, row_count, phase));
        }
    }
    if page_fits.is_empty() {
        return Some(None);
    }

    let mut internal_zero_stops = Vec::new();
    let mut run_start = None;
    for (index, value) in projection.iter().enumerate() {
        if *value == 0 && run_start.is_none() {
            run_start = Some(index);
        } else if *value != 0 {
            if let Some(start) = run_start.take() {
                if start > 0 && index < projection.len() {
                    internal_zero_stops.push(index);
                }
            }
        }
    }
    let symmetric_span = height - 2 * top_inset.min(bottom_inset);
    let minimum_pitch =
        3_usize.max((body_component_height(&components, 50.0) * 0.8).round_ties_even() as usize);
    let content_mean = content.iter().sum::<f64>() / content.len() as f64;
    let mut candidates = Vec::new();
    for (pitch, row_count, phase) in page_fits {
        let pair_count = content.len() - pitch;
        let first = &content[..pair_count];
        let second = &content[pitch..];
        let first_mean = first.iter().sum::<f64>() / pair_count as f64;
        let second_mean = second.iter().sum::<f64>() / pair_count as f64;
        let mut first_square_sum = 0.0;
        let mut second_square_sum = 0.0;
        let mut product_sum = 0.0;
        let mut absolute_difference_sum = 0.0;
        for (first_value, second_value) in first.iter().zip(second) {
            let first_delta = *first_value - first_mean;
            let second_delta = *second_value - second_mean;
            first_square_sum += first_delta * first_delta;
            second_square_sum += second_delta * second_delta;
            product_sum += first_delta * second_delta;
            absolute_difference_sum += (*first_value - *second_value).abs();
        }
        if first_square_sum == 0.0 || second_square_sum == 0.0 {
            continue;
        }
        let correlation = product_sum / (first_square_sum * second_square_sum).sqrt();
        let normalized_difference =
            (absolute_difference_sum / pair_count as f64) / content_mean.max(1.0);
        let repeated_rows = symmetric_span as f64 / pitch as f64;
        let repeat_fraction = (repeated_rows - repeated_rows.round_ties_even()).abs();
        let zero_periodicity = if internal_zero_stops.len() >= 2 {
            let (cosine, sine) =
                internal_zero_stops
                    .iter()
                    .fold((0.0_f64, 0.0_f64), |sum, coordinate| {
                        let angle = 2.0 * std::f64::consts::PI * *coordinate as f64 / pitch as f64;
                        (sum.0 + angle.cos(), sum.1 + angle.sin())
                    });
            cosine.hypot(sine) / internal_zero_stops.len() as f64
        } else {
            0.0
        };
        let score =
            correlation - normalized_difference - 3.0 * repeat_fraction + 0.1 * zero_periodicity;
        candidates.push((score, pitch, row_count, phase));
    }
    if candidates.is_empty() {
        return Some(None);
    }
    let has_body_candidate = candidates
        .iter()
        .any(|candidate| candidate.1 >= minimum_pitch);
    let (score, pitch, row_count, phase) = candidates
        .into_iter()
        .filter(|candidate| !has_body_candidate || candidate.1 >= minimum_pitch)
        .max_by(|first, second| {
            first
                .0
                .total_cmp(&second.0)
                .then_with(|| first.1.cmp(&second.1))
        })
        .expect("non-empty candidate pool");

    let mut reset_ratios = Vec::new();
    for index in 1..row_count {
        let coordinate = phase + index * pitch;
        if coordinate == 0 || coordinate >= projection.len() {
            continue;
        }
        let left_peak = projection[coordinate.saturating_sub(pitch)..coordinate]
            .iter()
            .copied()
            .max()
            .unwrap_or(0);
        let right_peak = projection[coordinate..(coordinate + pitch).min(projection.len())]
            .iter()
            .copied()
            .max()
            .unwrap_or(0);
        let smaller_peak = left_peak.min(right_peak);
        if smaller_peak == 0 {
            continue;
        }
        let boundary_ink = projection[coordinate - 1].min(projection[coordinate]);
        reset_ratios.push(boundary_ink as f64 / smaller_peak as f64);
    }
    let good_resets = reset_ratios.iter().filter(|ratio| **ratio <= 0.55).count();
    let reset_supported = good_resets >= 2
        && reset_ratios.len() == row_count - 1
        && good_resets as f64 / reset_ratios.len() as f64 >= 0.75;
    let sparse_three_row_supported = row_count == 3
        && pitch >= minimum_pitch
        && components.len() as f64 / row_count as f64 <= 3.0;
    if score <= -1.0 || !(reset_supported || sparse_three_row_supported) {
        return Some(None);
    }
    Some(Some(RegularRowGrid {
        pitch,
        phase,
        row_count,
        score,
    }))
}

pub fn best_row_grid_boundary(
    mask: &[u8],
    width: usize,
    height: usize,
    bbox: [usize; 4],
    row_grid: Option<RegularRowGrid>,
) -> Option<Option<usize>> {
    if width == 0
        || height == 0
        || mask.len() != width.checked_mul(height)?
        || bbox[0] >= bbox[2]
        || bbox[1] >= bbox[3]
        || bbox[2] > width
        || bbox[3] > height
    {
        return None;
    }
    let Some(row_grid) = row_grid else {
        return Some(None);
    };
    if row_grid.pitch == 0 || row_grid.row_count < 2 {
        return None;
    }
    let mut occupied_first = None;
    let mut occupied_last = None;
    for (local_row, row) in (bbox[1]..bbox[3]).enumerate() {
        if mask[row * width + bbox[0]..row * width + bbox[2]]
            .iter()
            .any(|value| *value != 0)
        {
            occupied_first.get_or_insert(local_row);
            occupied_last = Some(local_row);
        }
    }
    if let (Some(first), Some(last)) = (occupied_first, occupied_last) {
        if last - first + 1 <= row_grid.pitch {
            return Some(None);
        }
    }

    let relative_top = bbox[1] as isize - row_grid.phase as isize;
    let relative_bottom = bbox[3] as isize - row_grid.phase as isize;
    let pitch = row_grid.pitch as f64;
    let first_index = 1_isize.max((relative_top as f64 / pitch).floor() as isize + 1);
    let last_index =
        (row_grid.row_count as isize - 1).min((relative_bottom as f64 / pitch).ceil() as isize - 1);
    let mut best: Option<(f64, f64, usize)> = None;
    for index in first_index..=last_index {
        let coordinate = row_grid.phase as isize + index * row_grid.pitch as isize;
        if coordinate <= bbox[1] as isize || coordinate >= bbox[3] as isize {
            continue;
        }
        let coordinate = coordinate as usize;
        let before = (bbox[1]..coordinate)
            .map(|row| {
                mask[row * width + bbox[0]..row * width + bbox[2]]
                    .iter()
                    .filter(|value| **value != 0)
                    .count()
            })
            .sum::<usize>();
        let after = (coordinate..bbox[3])
            .map(|row| {
                mask[row * width + bbox[0]..row * width + bbox[2]]
                    .iter()
                    .filter(|value| **value != 0)
                    .count()
            })
            .sum::<usize>();
        if before == 0 || after == 0 {
            continue;
        }
        let balance = before.min(after) as f64 / before.max(after) as f64;
        let center_distance = ((bbox[1] + bbox[3]) as f64 / 2.0 - coordinate as f64).abs();
        let candidate = (balance, -center_distance, coordinate);
        if best.is_none_or(|selected| {
            candidate
                .0
                .total_cmp(&selected.0)
                .then_with(|| candidate.1.total_cmp(&selected.1))
                .then_with(|| candidate.2.cmp(&selected.2))
                .is_gt()
        }) {
            best = Some(candidate);
        }
    }
    Some(best.map(|candidate| candidate.2))
}

pub fn protected_upper_boundary(
    mask: &[u8],
    diacritic_guard: Option<&[u8]>,
    width: usize,
    height: usize,
    bbox: [usize; 4],
    guard_start: usize,
    guard_stop: usize,
) -> Option<bool> {
    if width == 0
        || height == 0
        || mask.len() != width.checked_mul(height)?
        || bbox[0] >= bbox[2]
        || bbox[1] >= bbox[3]
        || bbox[2] > width
        || bbox[3] > height
        || guard_start < bbox[1]
        || guard_start >= guard_stop
        || guard_stop > bbox[3]
    {
        return None;
    }
    let Some(diacritic_guard) = diacritic_guard else {
        return Some(false);
    };
    if diacritic_guard.len() != mask.len() {
        return None;
    }
    let local_width = bbox[2] - bbox[0];
    let mut bridge_columns = vec![false; local_width];
    for row in guard_start..guard_stop {
        for (column, selected) in bridge_columns.iter_mut().enumerate() {
            *selected |= diacritic_guard[row * width + bbox[0] + column] != 0;
        }
    }
    if !bridge_columns.iter().any(|value| *value) {
        return Some(false);
    }

    let mut bridge_start = guard_start;
    while bridge_start > bbox[1]
        && bridge_columns.iter().enumerate().any(|(column, selected)| {
            *selected && diacritic_guard[(bridge_start - 1) * width + bbox[0] + column] != 0
        })
    {
        bridge_start -= 1;
    }
    let lookback = 3_usize.max(2 * (guard_stop - bridge_start));
    let upper_start = bbox[1].max(bridge_start.saturating_sub(lookback));
    let mut upper_columns = vec![false; local_width];
    let mut upper_ink_columns = vec![false; local_width];
    for row in upper_start..bridge_start {
        for column in 0..local_width {
            let x = bbox[0] + column;
            if mask[row * width + x] == 0 {
                continue;
            }
            upper_ink_columns[column] = true;
            let row_start = row.saturating_sub(1).max(upper_start);
            let row_stop = (row + 1).min(bridge_start.saturating_sub(1));
            let column_start = column.saturating_sub(1);
            let column_stop = (column + 1).min(local_width - 1);
            'neighbor: for neighbor_row in row_start..=row_stop {
                for neighbor_column in column_start..=column_stop {
                    if neighbor_row == row && neighbor_column == column {
                        continue;
                    }
                    if mask[neighbor_row * width + bbox[0] + neighbor_column] != 0 {
                        upper_columns[column] = true;
                        break 'neighbor;
                    }
                }
            }
        }
    }
    for column in 0..local_width {
        upper_columns[column] |= upper_ink_columns[column] && bridge_columns[column];
    }
    let guarded_upper_columns = upper_columns
        .iter()
        .zip(&bridge_columns)
        .filter(|(upper, bridge)| **upper && **bridge)
        .count();
    let upper_count = upper_columns.iter().filter(|value| **value).count();
    Some(guarded_upper_columns as f64 / upper_count.max(1) as f64 >= 0.6)
}

pub fn has_body_evidence_on_both_sides(
    components: &[ConnectedComponent],
    bbox: [usize; 4],
    coordinate: usize,
    body_height: f64,
) -> Option<bool> {
    if bbox[0] >= bbox[2]
        || bbox[1] >= bbox[3]
        || coordinate < bbox[1]
        || coordinate > bbox[3]
        || !body_height.is_finite()
        || body_height < 0.0
    {
        return None;
    }
    let minimum_height = 2_usize.max((body_height * 0.5).round_ties_even() as usize);
    let mut above = false;
    let mut below = false;
    for component in components {
        let left = component.bbox[0].max(bbox[0]);
        let top = component.bbox[1].max(bbox[1]);
        let right = component.bbox[2].min(bbox[2]);
        let bottom = component.bbox[3].min(bbox[3]);
        if left >= right || top >= bottom || bottom - top < minimum_height {
            continue;
        }
        if (top + bottom) as f64 / 2.0 < coordinate as f64 {
            above = true;
        } else {
            below = true;
        }
        if above && below {
            return Some(true);
        }
    }
    Some(false)
}

pub fn best_row_valley(
    mask: &[u8],
    width: usize,
    height: usize,
    bbox: [usize; 4],
    body_height: f64,
    body_evidence_height: f64,
    components: &[ConnectedComponent],
    diacritic_guard: Option<&[u8]>,
) -> Option<Option<usize>> {
    if width == 0
        || height == 0
        || mask.len() != width.checked_mul(height)?
        || bbox[0] >= bbox[2]
        || bbox[1] >= bbox[3]
        || bbox[2] > width
        || bbox[3] > height
        || !body_height.is_finite()
        || body_height < 0.0
        || !body_evidence_height.is_finite()
        || body_evidence_height < 0.0
        || diacritic_guard.is_some_and(|guard| guard.len() != mask.len())
    {
        return None;
    }
    let projection: Vec<usize> = (bbox[1]..bbox[3])
        .map(|row| {
            mask[row * width + bbox[0]..row * width + bbox[2]]
                .iter()
                .filter(|value| **value != 0)
                .count()
        })
        .collect();
    let Some(first_occupied) = projection.iter().position(|value| *value != 0) else {
        return Some(None);
    };
    let last_occupied = projection
        .iter()
        .rposition(|value| *value != 0)
        .expect("occupied projection has a final row");
    let content_top = bbox[1] + first_occupied;
    let content_bottom = bbox[1] + last_occupied + 1;
    let minimum_span = 3_usize.max((body_height * 0.55).round_ties_even() as usize);
    let radius = 3_usize.max(body_height.round_ties_even() as usize);
    let local_components: Vec<&ConnectedComponent> = components
        .iter()
        .filter(|component| {
            component.bbox[0] < bbox[2]
                && bbox[0] < component.bbox[2]
                && component.bbox[1] < bbox[3]
                && bbox[1] < component.bbox[3]
        })
        .collect();
    let first_coordinate = content_top.saturating_add(minimum_span);
    let last_coordinate = content_bottom.saturating_sub(minimum_span);
    if first_coordinate > last_coordinate {
        return Some(None);
    }
    let mut candidates = Vec::new();
    for coordinate in first_coordinate..=last_coordinate {
        let has_upper = local_components.iter().any(|component| {
            (component.bbox[1] + component.bbox[3]) as f64 / 2.0 < coordinate as f64
        });
        let has_lower = local_components.iter().any(|component| {
            (component.bbox[1] + component.bbox[3]) as f64 / 2.0 >= coordinate as f64
        });
        if !has_upper || !has_lower {
            continue;
        }
        let local_coordinate = coordinate - bbox[1];
        let left_start = local_coordinate.saturating_sub(radius);
        let right_stop = projection.len().min(local_coordinate + radius);
        let left_peak = projection[left_start..local_coordinate]
            .iter()
            .copied()
            .max()
            .unwrap_or(0);
        let right_peak = projection[local_coordinate..right_stop]
            .iter()
            .copied()
            .max()
            .unwrap_or(0);
        let smaller_peak = left_peak.min(right_peak);
        if smaller_peak == 0 {
            continue;
        }
        let valley = projection[local_coordinate - 1].min(projection[local_coordinate]);
        let valley_ratio = valley as f64 / smaller_peak as f64;
        let ending_width: usize = local_components
            .iter()
            .filter(|component| component.bbox[3] == coordinate)
            .map(|component| component.bbox[2] - component.bbox[0])
            .sum();
        let starting_width: usize = local_components
            .iter()
            .filter(|component| component.bbox[1] == coordinate)
            .map(|component| component.bbox[2] - component.bbox[0])
            .sum();
        let endpoint_width_ratio =
            ending_width.min(starting_width) as f64 / (bbox[2] - bbox[0]).max(1) as f64;
        let endpoint_supported =
            ending_width > 0 && starting_width > 0 && endpoint_width_ratio >= 0.03;
        let before: usize = projection[..local_coordinate].iter().sum();
        let after: usize = projection[local_coordinate..].iter().sum();
        if before == 0 || after == 0 {
            continue;
        }
        let balance = before.min(after) as f64 / before.max(after) as f64;
        if balance < 0.08 {
            continue;
        }
        let body_evidence = || {
            has_body_evidence_on_both_sides(components, bbox, coordinate, body_evidence_height)
                .unwrap_or(false)
        };
        if endpoint_supported {
            if valley_ratio > 0.6 {
                continue;
            }
        } else if valley_ratio > 0.3 && (valley_ratio > 0.4 || balance < 0.2 || !body_evidence()) {
            continue;
        }
        if balance < 0.2 && !body_evidence() {
            continue;
        }

        let local_width = bbox[2] - bbox[0];
        let mut upper_columns = vec![false; local_width];
        let mut lower_columns = vec![false; local_width];
        for component in &local_components {
            let component_height = component.bbox[3] - component.bbox[1];
            if component.pixels < 4 || component_height < 3 {
                continue;
            }
            let left = bbox[0].max(component.bbox[0]) - bbox[0];
            let right = bbox[2].min(component.bbox[2]) - bbox[0];
            if right <= left {
                continue;
            }
            let component_center = (component.bbox[1] + component.bbox[3]) as f64 / 2.0;
            let target = if component_center < coordinate as f64 {
                &mut upper_columns
            } else {
                &mut lower_columns
            };
            target[left..right].fill(true);
        }
        let upper_width = upper_columns.iter().filter(|value| **value).count();
        let lower_width = lower_columns.iter().filter(|value| **value).count();
        let minimum_row_width = 8_usize
            .max((body_evidence_height * 1.5).round_ties_even() as usize)
            .max((local_width as f64 * 0.08).round_ties_even() as usize);
        if upper_width.min(lower_width) < minimum_row_width {
            continue;
        }
        if protected_upper_boundary(
            mask,
            diacritic_guard,
            width,
            height,
            bbox,
            bbox[1].max(coordinate - 1),
            bbox[3].min(coordinate + 1),
        )
        .unwrap_or(false)
        {
            continue;
        }
        let center_distance =
            ((content_top + content_bottom) as f64 / 2.0 - coordinate as f64).abs();
        let mut score = 1.0 - valley_ratio + 0.2 * balance;
        if endpoint_supported {
            score += 1.0 + endpoint_width_ratio;
        }
        candidates.push((score, -center_distance, coordinate));
    }
    Some(
        candidates
            .into_iter()
            .max_by(|first, second| {
                first
                    .0
                    .total_cmp(&second.0)
                    .then_with(|| first.1.total_cmp(&second.1))
                    .then_with(|| first.2.cmp(&second.2))
            })
            .map(|candidate| candidate.2),
    )
}

pub fn supports_imbalanced_row_gap(
    components: &[ConnectedComponent],
    bbox: [usize; 4],
    gap_start: usize,
    gap_stop: usize,
    before_pixels: usize,
    after_pixels: usize,
) -> Option<bool> {
    if bbox[0] >= bbox[2] || bbox[1] >= bbox[3] || gap_start < bbox[1] || gap_stop > bbox[3] {
        return None;
    }
    if gap_stop <= gap_start {
        return Some(false);
    }
    let minority_before = before_pixels < after_pixels;
    let minority: Vec<&ConnectedComponent> = components
        .iter()
        .filter(|component| {
            component.bbox[0] < bbox[2]
                && bbox[0] < component.bbox[2]
                && component.bbox[1] < bbox[3]
                && bbox[1] < component.bbox[3]
                && if minority_before {
                    component.bbox[3] <= gap_start
                } else {
                    component.bbox[1] >= gap_stop
                }
                && component.pixels >= 4
                && component.bbox[3] - component.bbox[1] >= 2
        })
        .collect();
    if minority.len() < 2 {
        return Some(false);
    }
    let heights: Vec<usize> = minority
        .iter()
        .map(|component| component.bbox[3] - component.bbox[1])
        .collect();
    let local_body_height = linear_percentile(&heights, 50.0);
    let minority_left = minority
        .iter()
        .map(|component| component.bbox[0])
        .min()
        .expect("minority has two components");
    let minority_top = minority
        .iter()
        .map(|component| component.bbox[1])
        .min()
        .expect("minority has two components");
    let minority_right = minority
        .iter()
        .map(|component| component.bbox[2])
        .max()
        .expect("minority has two components");
    let minority_bottom = minority
        .iter()
        .map(|component| component.bbox[3])
        .max()
        .expect("minority has two components");
    let minority_pixels: usize = minority.iter().map(|component| component.pixels).sum();
    Some(
        gap_stop - gap_start >= 4_usize.max((1.5 * local_body_height).round_ties_even() as usize)
            && minority_bottom - minority_top
                <= 4_usize.max((2.5 * local_body_height).round_ties_even() as usize)
            && minority_right - minority_left
                >= 8_usize.max((1.5 * local_body_height).round_ties_even() as usize)
            && minority_pixels
                >= 24_usize.max((4.0 * local_body_height).round_ties_even() as usize),
    )
}

pub fn supports_projected_column_gap(
    rules: &[RuleDraftSummary],
    bbox: [usize; 4],
    gap_start: usize,
    gap_stop: usize,
    body_height: f64,
) -> Option<bool> {
    if bbox[0] >= bbox[2]
        || bbox[1] >= bbox[3]
        || gap_start < bbox[0]
        || gap_stop > bbox[2]
        || gap_stop < gap_start
        || !body_height.is_finite()
        || body_height < 0.0
    {
        return None;
    }
    let matching: Vec<&RuleDraftSummary> = rules
        .iter()
        .filter(|rule| {
            !rule.horizontal
                && gap_start as f64 <= (rule.bbox[0] + rule.bbox[2]) as f64 / 2.0
                && (rule.bbox[0] + rule.bbox[2]) as f64 / 2.0 <= gap_stop as f64
        })
        .collect();
    if matching.is_empty() {
        return Some(false);
    }
    let maximum_local_distance = 4_usize
        .max((2.0 * body_height).round_ties_even() as usize)
        .max(bbox[3] - bbox[1]);
    if matching.iter().any(|rule| {
        bbox[1]
            .saturating_sub(rule.bbox[3])
            .max(rule.bbox[1].saturating_sub(bbox[3]))
            <= maximum_local_distance
    }) {
        return Some(true);
    }
    Some(matching.len() >= 2)
}

pub fn best_separator(
    mask: &[u8],
    width: usize,
    height: usize,
    bbox: [usize; 4],
    row_minimum_gap: usize,
    column_minimum_gap: usize,
    components: &[ConnectedComponent],
    body_height: f64,
    rules: &[RuleDraftSummary],
    diacritic_guard: Option<&[u8]>,
    allow_non_rule_columns: bool,
    projected_column_rules: &[RuleDraftSummary],
    prefer_rows: bool,
) -> Option<Option<SeparatorChoice>> {
    if width == 0
        || height == 0
        || mask.len() != width.checked_mul(height)?
        || bbox[0] >= bbox[2]
        || bbox[1] >= bbox[3]
        || bbox[2] > width
        || bbox[3] > height
        || !body_height.is_finite()
        || body_height < 0.0
        || diacritic_guard.is_some_and(|guard| guard.len() != mask.len())
    {
        return None;
    }
    let row_projection: Vec<usize> = (bbox[1]..bbox[3])
        .map(|row| {
            mask[row * width + bbox[0]..row * width + bbox[2]]
                .iter()
                .filter(|value| **value != 0)
                .count()
        })
        .collect();
    let column_projection: Vec<usize> = (bbox[0]..bbox[2])
        .map(|column| {
            (bbox[1]..bbox[3])
                .filter(|row| mask[row * width + column] != 0)
                .count()
        })
        .collect();
    let content_height = match (
        row_projection.iter().position(|value| *value != 0),
        row_projection.iter().rposition(|value| *value != 0),
    ) {
        (Some(first), Some(last)) => last - first + 1,
        _ => bbox[3] - bbox[1],
    };
    let content_width = match (
        column_projection.iter().position(|value| *value != 0),
        column_projection.iter().rposition(|value| *value != 0),
    ) {
        (Some(first), Some(last)) => last - first + 1,
        _ => bbox[2] - bbox[0],
    };
    let total_ink: usize = row_projection.iter().sum();
    if total_ink == 0 {
        return Some(None);
    }

    #[derive(Clone, Copy)]
    struct Candidate {
        rule_seam: bool,
        preferred_row: bool,
        score: f64,
        rows: bool,
        start: usize,
        separator: [usize; 4],
    }

    fn intersection(rule: [usize; 4], bbox: [usize; 4]) -> Option<[usize; 4]> {
        let value = [
            rule[0].max(bbox[0]),
            rule[1].max(bbox[1]),
            rule[2].min(bbox[2]),
            rule[3].min(bbox[3]),
        ];
        (value[0] < value[2] && value[1] < value[3]).then_some(value)
    }

    let mut candidates = Vec::new();
    for (rows, projection, offset, minimum_gap) in [
        (true, &row_projection, bbox[1], row_minimum_gap),
        (false, &column_projection, bbox[0], column_minimum_gap),
    ] {
        let mut cumulative = Vec::with_capacity(projection.len());
        let mut running = 0_usize;
        for value in projection {
            running += value;
            cumulative.push(running);
        }
        let mut local_start = 0;
        while local_start < projection.len() {
            if projection[local_start] != 0 {
                local_start += 1;
                continue;
            }
            let mut local_stop = local_start + 1;
            while local_stop < projection.len() && projection[local_stop] == 0 {
                local_stop += 1;
            }
            let start = offset + local_start;
            let stop = offset + local_stop;
            let is_rule_seam = rules.iter().any(|rule| {
                let Some(overlap) = intersection(rule.bbox, bbox) else {
                    return false;
                };
                if rows {
                    rule.horizontal
                        && rule.bbox[1] < stop
                        && start < rule.bbox[3]
                        && overlap[2] - overlap[0]
                            >= (content_width as f64 * 0.8).round_ties_even() as usize
                } else {
                    !rule.horizontal
                        && rule.bbox[0] < stop
                        && start < rule.bbox[2]
                        && overlap[3] - overlap[1]
                            >= (content_height as f64 * 0.8).round_ties_even() as usize
                }
            });
            if stop - start < minimum_gap && !is_rule_seam {
                local_start = local_stop;
                continue;
            }
            if !rows
                && !is_rule_seam
                && !allow_non_rule_columns
                && !supports_projected_column_gap(
                    projected_column_rules,
                    bbox,
                    start,
                    stop,
                    body_height,
                )
                .unwrap_or(false)
            {
                local_start = local_stop;
                continue;
            }
            let before = if local_start > 0 {
                cumulative[local_start - 1]
            } else {
                0
            };
            let after = total_ink
                - if local_stop > 0 {
                    cumulative[local_stop - 1]
                } else {
                    0
                };
            if before == 0 || after == 0 {
                local_start = local_stop;
                continue;
            }
            let balance = before.min(after) as f64 / before.max(after) as f64;
            if rows
                && !is_rule_seam
                && balance < 0.2
                && !has_body_evidence_on_both_sides(components, bbox, start, body_height)
                    .unwrap_or(false)
                && !supports_imbalanced_row_gap(components, bbox, start, stop, before, after)
                    .unwrap_or(false)
            {
                local_start = local_stop;
                continue;
            }
            if rows
                && !is_rule_seam
                && protected_upper_boundary(mask, diacritic_guard, width, height, bbox, start, stop)
                    .unwrap_or(false)
            {
                local_start = local_stop;
                continue;
            }
            let span = if rows {
                bbox[3] - bbox[1]
            } else {
                bbox[2] - bbox[0]
            };
            let score = (stop - start) as f64 / span.max(1) as f64 + 0.25 * balance;
            candidates.push(Candidate {
                rule_seam: is_rule_seam,
                preferred_row: prefer_rows && rows,
                score,
                rows,
                start,
                separator: if rows {
                    [bbox[0], start, bbox[2], stop]
                } else {
                    [start, bbox[1], stop, bbox[3]]
                },
            });
            local_start = local_stop;
        }
    }
    if candidates.is_empty() {
        return Some(None);
    }

    fn general_order(first: &Candidate, second: &Candidate) -> std::cmp::Ordering {
        first
            .rule_seam
            .cmp(&second.rule_seam)
            .then_with(|| first.preferred_row.cmp(&second.preferred_row))
            .then_with(|| first.score.total_cmp(&second.score))
            .then_with(|| first.rows.cmp(&second.rows))
            .then_with(|| second.start.cmp(&first.start))
    }

    let selected = if prefer_rows && candidates.iter().any(|candidate| candidate.rows) {
        candidates
            .iter()
            .filter(|candidate| candidate.rows)
            .max_by(|first, second| {
                let first_height = first.separator[3] - first.separator[1];
                let second_height = second.separator[3] - second.separator[1];
                let bbox_center = (bbox[1] + bbox[3]) as f64 / 2.0;
                let first_distance =
                    ((first.separator[1] + first.separator[3]) as f64 / 2.0 - bbox_center).abs();
                let second_distance =
                    ((second.separator[1] + second.separator[3]) as f64 / 2.0 - bbox_center).abs();
                first_height
                    .cmp(&second_height)
                    .then_with(|| second_distance.total_cmp(&first_distance))
                    .then_with(|| first.rule_seam.cmp(&second.rule_seam))
                    .then_with(|| first.score.total_cmp(&second.score))
                    .then_with(|| second.start.cmp(&first.start))
            })
            .expect("row candidate exists")
    } else {
        candidates
            .iter()
            .max_by(|first, second| general_order(first, second))
            .expect("candidate exists")
    };
    Some(Some(SeparatorChoice {
        rows: selected.rows,
        bbox: selected.separator,
        rule_seam: selected.rule_seam,
    }))
}

pub fn layout_partition_evidence(
    mask: &[u8],
    width: usize,
    height: usize,
    components: &[ConnectedComponent],
    runs: &[ConnectedComponentRun],
    maximum_evidence_components: usize,
) -> Option<(Vec<u8>, Vec<ConnectedComponent>)> {
    if width == 0 || height == 0 || mask.len() != width.checked_mul(height)? {
        return None;
    }
    if components.len() <= maximum_evidence_components {
        return Some((mask.to_vec(), components.to_vec()));
    }
    let meaningful_indexes: Vec<usize> = components
        .iter()
        .enumerate()
        .filter(|(_, component)| {
            component.pixels >= 4 && component.bbox[3] - component.bbox[1] >= 3
        })
        .map(|(index, _)| index)
        .collect();
    if meaningful_indexes.is_empty() {
        return Some((mask.to_vec(), components.to_vec()));
    }
    let mut selected = vec![false; components.len()];
    for index in &meaningful_indexes {
        selected[*index] = true;
    }
    let mut evidence = vec![0_u8; mask.len()];
    for run in runs {
        if run.component_index >= components.len()
            || run.row >= height
            || run.start >= run.stop
            || run.stop > width
        {
            return None;
        }
        if selected[run.component_index] {
            evidence[run.row * width + run.start..run.row * width + run.stop].fill(1);
        }
    }
    let meaningful = meaningful_indexes
        .into_iter()
        .map(|index| components[index])
        .collect();
    Some((evidence, meaningful))
}

fn recursive_partition_internal(
    mask: &[u8],
    width: usize,
    height: usize,
    components: &[ConnectedComponent],
    runs: &[ConnectedComponentRun],
    config: PartitionConfig,
    rules: &[RuleDraftSummary],
    diacritic_guard: Option<&[u8]>,
    row_grid: Option<RegularRowGrid>,
    nonstructural_rule_boxes: &[[usize; 4]],
    cover_separators: bool,
    partition_rgb: Option<&[u8]>,
    physical_fallback: Option<&[u8]>,
) -> Option<(Vec<PartitionNode>, Vec<u8>)> {
    if width == 0
        || height == 0
        || mask.len() != width.checked_mul(height)?
        || config.max_nodes == 0
        || diacritic_guard.is_some_and(|guard| guard.len() != mask.len())
        || partition_rgb.is_some_and(|rgb| rgb.len() != mask.len().saturating_mul(3))
        || physical_fallback.is_some_and(|fallback| fallback.len() != mask.len())
    {
        return None;
    }
    let original_component_count = components.len();
    let (mut working_mask, root_components) =
        layout_partition_evidence(mask, width, height, components, runs, config.max_nodes)?;
    let cover_separators = cover_separators || root_components.len() != original_component_count;
    let mut working_guard = diacritic_guard
        .map(<[u8]>::to_vec)
        .unwrap_or_else(|| vec![0_u8; mask.len()]);
    let root_body_component_height = body_component_height(&root_components, 75.0);
    let root_valley_body_height = body_component_height(&root_components, 50.0);
    let mut nodes = vec![PartitionNode {
        bbox: [0, 0, width, height],
        depth: 0,
        parent_index: None,
        rows: None,
        child_indexes: [None, None],
        separator: None,
        split_coordinate: None,
        stop_reason: None,
        rule_partition: false,
        row_rule_top: false,
        row_rule_bottom: false,
    }];
    let mut stack = vec![(0_usize, root_components, rules.to_vec())];
    while let Some((node_index, local_components, local_rules)) = stack.pop() {
        let node_bbox = nodes[node_index].bbox;
        let node_depth = nodes[node_index].depth;
        let node_rule_partition = nodes[node_index].rule_partition;
        let node_row_rule_top = nodes[node_index].row_rule_top;
        let node_row_rule_bottom = nodes[node_index].row_rule_bottom;
        let mut local_components = local_components;
        if let Some(partition_rgb) = partition_rgb {
            let crop_width = node_bbox[2] - node_bbox[0];
            let crop_height = node_bbox[3] - node_bbox[1];
            let mut crop_rgb = Vec::with_capacity(crop_width * crop_height * 3);
            let mut inherited_mask = Vec::with_capacity(crop_width * crop_height);
            let mut fallback_mask = Vec::with_capacity(crop_width * crop_height);
            for row in node_bbox[1]..node_bbox[3] {
                crop_rgb.extend_from_slice(
                    &partition_rgb
                        [(row * width + node_bbox[0]) * 3..(row * width + node_bbox[2]) * 3],
                );
                inherited_mask.extend_from_slice(
                    &working_mask[row * width + node_bbox[0]..row * width + node_bbox[2]],
                );
                if let Some(physical_fallback) = physical_fallback {
                    fallback_mask.extend_from_slice(
                        &physical_fallback[row * width + node_bbox[0]..row * width + node_bbox[2]],
                    );
                }
            }
            if physical_fallback.is_none() {
                fallback_mask.clone_from(&inherited_mask);
            }
            let (mut local_mask, _) = select_layout_foreground_mask(
                &crop_rgb,
                crop_width,
                crop_height,
                &fallback_mask,
                true,
            )?;
            for (selected, inherited) in local_mask.iter_mut().zip(&inherited_mask) {
                *selected |= *inherited;
            }
            for rule in &local_rules {
                let intersection = [
                    rule.bbox[0].max(node_bbox[0]),
                    rule.bbox[1].max(node_bbox[1]),
                    rule.bbox[2].min(node_bbox[2]),
                    rule.bbox[3].min(node_bbox[3]),
                ];
                if intersection[0] >= intersection[2] || intersection[1] >= intersection[3] {
                    continue;
                }
                for row in intersection[1] - node_bbox[1]..intersection[3] - node_bbox[1] {
                    local_mask[row * crop_width + intersection[0] - node_bbox[0]
                        ..row * crop_width + intersection[2] - node_bbox[0]]
                        .fill(0);
                }
            }
            let (crop_components, crop_runs) =
                connected_component_runs(&local_mask, crop_width, crop_height)?;
            let (local_mask, crop_components) = layout_partition_evidence(
                &local_mask,
                crop_width,
                crop_height,
                &crop_components,
                &crop_runs,
                config.max_nodes,
            )?;
            for (local_row, row) in (node_bbox[1]..node_bbox[3]).enumerate() {
                working_mask[row * width + node_bbox[0]..row * width + node_bbox[2]]
                    .copy_from_slice(
                        &local_mask[local_row * crop_width..(local_row + 1) * crop_width],
                    );
            }
            local_components = crop_components
                .into_iter()
                .map(|component| ConnectedComponent {
                    bbox: [
                        component.bbox[0] + node_bbox[0],
                        component.bbox[1] + node_bbox[1],
                        component.bbox[2] + node_bbox[0],
                        component.bbox[3] + node_bbox[1],
                    ],
                    pixels: component.pixels,
                })
                .collect();
            let guarded = guard_diacritic_gaps(&local_mask, crop_width, crop_height)?;
            for (local_row, row) in (node_bbox[1]..node_bbox[3]).enumerate() {
                for local_column in 0..crop_width {
                    let local_index = local_row * crop_width + local_column;
                    working_guard[row * width + node_bbox[0] + local_column] =
                        u8::from(guarded[local_index] != 0 && local_mask[local_index] == 0);
                }
            }
        }
        let body_height = if partition_rgb.is_some() {
            body_component_height(&local_components, 75.0)
        } else {
            root_body_component_height
        };
        let valley_height = if partition_rgb.is_some() {
            body_component_height(&local_components, 50.0)
        } else {
            root_valley_body_height
        };
        let mut effective_column_gap = config
            .min_safe_gap
            .max((body_height * 0.8).round_ties_even() as usize);
        if node_bbox[0] != 0 || node_bbox[2] != width {
            effective_column_gap = effective_column_gap
                .max(((node_bbox[2] - node_bbox[0]) as f64 * 0.12).round_ties_even() as usize);
        }
        let local_pixels: usize = (node_bbox[1]..node_bbox[3])
            .map(|row| {
                working_mask[row * width + node_bbox[0]..row * width + node_bbox[2]]
                    .iter()
                    .filter(|value| **value != 0)
                    .count()
            })
            .sum();
        if local_pixels == 0 {
            nodes[node_index].stop_reason = Some(if nodes[node_index].parent_index.is_none() {
                PartitionStopReason::Empty
            } else {
                PartitionStopReason::Atomic
            });
            continue;
        }
        let decision = best_separator(
            &working_mask,
            width,
            height,
            node_bbox,
            1,
            effective_column_gap,
            &local_components,
            body_height,
            &local_rules,
            Some(&working_guard),
            !node_rule_partition,
            if node_rule_partition { rules } else { &[] },
            true,
        )?;
        let mut split_coordinate =
            best_row_grid_boundary(&working_mask, width, height, node_bbox, row_grid)?;
        let rows;
        let mut separator;
        if decision.is_some_and(|value| value.rule_seam) {
            split_coordinate = None;
            let value = decision.expect("rule seam decision exists");
            rows = value.rows;
            separator = Some(value.bbox);
        } else if decision.is_some_and(|value| value.rows) {
            split_coordinate = None;
            let value = decision.expect("row decision exists");
            rows = true;
            separator = Some(value.bbox);
        } else if split_coordinate.is_some() {
            rows = true;
            separator = None;
        } else if node_rule_partition {
            split_coordinate = best_row_valley(
                &working_mask,
                width,
                height,
                node_bbox,
                valley_height,
                body_height,
                &local_components,
                Some(&working_guard),
            )?;
            if split_coordinate.is_none() {
                let Some(value) = decision else {
                    nodes[node_index].stop_reason = Some(PartitionStopReason::Atomic);
                    continue;
                };
                rows = value.rows;
                separator = Some(value.bbox);
            } else {
                rows = true;
                separator = None;
            }
        } else if decision.is_none() {
            split_coordinate = best_row_valley(
                &working_mask,
                width,
                height,
                node_bbox,
                valley_height,
                body_height,
                &local_components,
                Some(&working_guard),
            )?;
            if split_coordinate.is_none() {
                nodes[node_index].stop_reason = Some(PartitionStopReason::Atomic);
                continue;
            }
            rows = true;
            separator = None;
        } else {
            let value = decision.expect("remaining decision exists");
            rows = value.rows;
            separator = Some(value.bbox);
        }
        if node_depth >= config.max_depth || nodes.len() + 2 > config.max_nodes {
            nodes[node_index].stop_reason = Some(PartitionStopReason::Limit);
            continue;
        }
        let child_boxes = if let Some(coordinate) = split_coordinate {
            [
                [node_bbox[0], node_bbox[1], node_bbox[2], coordinate],
                [node_bbox[0], coordinate, node_bbox[2], node_bbox[3]],
            ]
        } else if rows {
            let value = separator.expect("row separator exists");
            if cover_separators {
                let midpoint = (value[1] + value[3]) / 2;
                [
                    [node_bbox[0], node_bbox[1], node_bbox[2], midpoint],
                    [node_bbox[0], midpoint, node_bbox[2], node_bbox[3]],
                ]
            } else {
                [
                    [node_bbox[0], node_bbox[1], node_bbox[2], value[1]],
                    [node_bbox[0], value[3], node_bbox[2], node_bbox[3]],
                ]
            }
        } else {
            let value = separator.expect("column separator exists");
            if cover_separators {
                let midpoint = (value[0] + value[2]) / 2;
                [
                    [node_bbox[0], node_bbox[1], midpoint, node_bbox[3]],
                    [midpoint, node_bbox[1], node_bbox[2], node_bbox[3]],
                ]
            } else {
                [
                    [node_bbox[0], node_bbox[1], value[0], node_bbox[3]],
                    [value[2], node_bbox[1], node_bbox[2], node_bbox[3]],
                ]
            }
        };
        let horizontal_rule_split = decision.is_some_and(|value| value.rule_seam)
            && rows
            && separator.is_some()
            && !nonstructural_rule_boxes.iter().any(|bbox| {
                let value = separator.expect("separator exists");
                bbox[1] < value[3] && value[1] < bbox[3]
            });
        let child_row_boundaries = if horizontal_rule_split {
            [(node_row_rule_top, true), (true, node_row_rule_bottom)]
        } else if rows {
            [(node_row_rule_top, false), (false, node_row_rule_bottom)]
        } else {
            [
                (node_row_rule_top, node_row_rule_bottom),
                (node_row_rule_top, node_row_rule_bottom),
            ]
        };
        let first_child_index = nodes.len();
        for child_offset in 0..2 {
            let (row_rule_top, row_rule_bottom) = child_row_boundaries[child_offset];
            nodes.push(PartitionNode {
                bbox: child_boxes[child_offset],
                depth: node_depth + 1,
                parent_index: Some(node_index),
                rows: None,
                child_indexes: [None, None],
                separator: None,
                split_coordinate: None,
                stop_reason: None,
                rule_partition: node_rule_partition || (row_rule_top && row_rule_bottom),
                row_rule_top,
                row_rule_bottom,
            });
        }
        nodes[node_index].rows = Some(rows);
        nodes[node_index].child_indexes = [Some(first_child_index), Some(first_child_index + 1)];
        nodes[node_index].separator = separator.take();
        nodes[node_index].split_coordinate = split_coordinate;

        for child_offset in (0..2).rev() {
            let child_index = first_child_index + child_offset;
            let child_bbox = nodes[child_index].bbox;
            let child_components = local_components
                .iter()
                .copied()
                .filter(|component| {
                    component.bbox[0] < child_bbox[2]
                        && child_bbox[0] < component.bbox[2]
                        && component.bbox[1] < child_bbox[3]
                        && child_bbox[1] < component.bbox[3]
                })
                .collect();
            let child_rules = local_rules
                .iter()
                .copied()
                .filter(|rule| {
                    rule.bbox[0] < child_bbox[2]
                        && child_bbox[0] < rule.bbox[2]
                        && rule.bbox[1] < child_bbox[3]
                        && child_bbox[1] < rule.bbox[3]
                })
                .collect();
            stack.push((child_index, child_components, child_rules));
        }
    }
    Some((nodes, working_mask))
}

pub fn recursive_partition_without_adaptive_rgb(
    mask: &[u8],
    width: usize,
    height: usize,
    components: &[ConnectedComponent],
    runs: &[ConnectedComponentRun],
    config: PartitionConfig,
    rules: &[RuleDraftSummary],
    diacritic_guard: Option<&[u8]>,
    row_grid: Option<RegularRowGrid>,
    nonstructural_rule_boxes: &[[usize; 4]],
    cover_separators: bool,
) -> Option<(Vec<PartitionNode>, Vec<u8>)> {
    recursive_partition_internal(
        mask,
        width,
        height,
        components,
        runs,
        config,
        rules,
        diacritic_guard,
        row_grid,
        nonstructural_rule_boxes,
        cover_separators,
        None,
        None,
    )
}

pub fn recursive_partition_with_adaptive_rgb(
    mask: &[u8],
    width: usize,
    height: usize,
    components: &[ConnectedComponent],
    runs: &[ConnectedComponentRun],
    config: PartitionConfig,
    rules: &[RuleDraftSummary],
    diacritic_guard: Option<&[u8]>,
    row_grid: Option<RegularRowGrid>,
    nonstructural_rule_boxes: &[[usize; 4]],
    cover_separators: bool,
    partition_rgb: &[u8],
    physical_fallback: Option<&[u8]>,
) -> Option<(Vec<PartitionNode>, Vec<u8>)> {
    recursive_partition_internal(
        mask,
        width,
        height,
        components,
        runs,
        config,
        rules,
        diacritic_guard,
        row_grid,
        nonstructural_rule_boxes,
        cover_separators,
        Some(partition_rgb),
        physical_fallback,
    )
}

pub fn fragment_components_for_leaves(
    components: &[ConnectedComponent],
    runs: &[ConnectedComponentRun],
    nodes: &[PartitionNode],
) -> Option<(Vec<ConnectedComponent>, Vec<ConnectedComponentRun>)> {
    if nodes.is_empty() || nodes[0].parent_index.is_some() {
        return None;
    }
    let mut component_runs = vec![Vec::<ConnectedComponentRun>::new(); components.len()];
    for run in runs {
        if run.component_index >= components.len() || run.start >= run.stop {
            return None;
        }
        component_runs[run.component_index].push(*run);
    }
    let mut group_indexes = std::collections::HashMap::<(usize, usize), usize>::new();
    let mut groups = Vec::<Vec<ConnectedComponentRun>>::new();
    for (component_index, source_runs) in component_runs.iter().enumerate() {
        for run in source_runs {
            let mut row_leaves = Vec::new();
            let mut stack = vec![0_usize];
            while let Some(node_index) = stack.pop() {
                let node = nodes.get(node_index)?;
                if !(node.bbox[1] <= run.row
                    && run.row < node.bbox[3]
                    && node.bbox[0] < run.stop
                    && run.start < node.bbox[2])
                {
                    continue;
                }
                match node.child_indexes {
                    [Some(first), Some(second)] => {
                        stack.push(second);
                        stack.push(first);
                    }
                    [None, None] => row_leaves.push(node_index),
                    _ => return None,
                }
            }
            let mut owned = 0_usize;
            for leaf_index in row_leaves {
                let leaf = &nodes[leaf_index];
                let start = run.start.max(leaf.bbox[0]);
                let stop = run.stop.min(leaf.bbox[2]);
                if start >= stop {
                    continue;
                }
                let group_index = *group_indexes
                    .entry((component_index, leaf_index))
                    .or_insert_with(|| {
                        groups.push(Vec::new());
                        groups.len() - 1
                    });
                groups[group_index].push(ConnectedComponentRun {
                    component_index: group_index,
                    row: run.row,
                    start,
                    stop,
                });
                owned += stop - start;
            }
            if owned != run.stop - run.start {
                return None;
            }
        }
    }
    let mut fragments: Vec<(ConnectedComponent, Vec<ConnectedComponentRun>, usize)> = groups
        .into_iter()
        .enumerate()
        .map(|(insertion_index, group_runs)| {
            let component = ConnectedComponent {
                bbox: [
                    group_runs.iter().map(|run| run.start).min().unwrap_or(0),
                    group_runs.iter().map(|run| run.row).min().unwrap_or(0),
                    group_runs.iter().map(|run| run.stop).max().unwrap_or(0),
                    group_runs.iter().map(|run| run.row + 1).max().unwrap_or(0),
                ],
                pixels: group_runs.iter().map(|run| run.stop - run.start).sum(),
            };
            (component, group_runs, insertion_index)
        })
        .collect();
    fragments.sort_by_key(|(component, _, insertion_index)| {
        (
            component.bbox[1],
            component.bbox[0],
            component.bbox[3],
            component.bbox[2],
            *insertion_index,
        )
    });
    let mut output_components = Vec::with_capacity(fragments.len());
    let mut output_runs = Vec::new();
    for (component_index, (component, group_runs, _)) in fragments.into_iter().enumerate() {
        output_components.push(component);
        output_runs.extend(group_runs.into_iter().map(|run| ConnectedComponentRun {
            component_index,
            ..run
        }));
    }
    Some((output_components, output_runs))
}

fn near_collinear_rule(
    horizontal: bool,
    bbox: [usize; 4],
    structural_bands: &[RuleDraftSummary],
    minimum_rule_length: usize,
) -> bool {
    let mut aligned = 0_usize;
    for candidate in structural_bands {
        if candidate.horizontal != horizontal {
            continue;
        }
        let (perpendicular_distance, axial_distance) = if horizontal {
            (
                candidate.bbox[1]
                    .saturating_sub(bbox[3])
                    .max(bbox[1].saturating_sub(candidate.bbox[3])),
                candidate.bbox[0]
                    .saturating_sub(bbox[2])
                    .max(bbox[0].saturating_sub(candidate.bbox[2])),
            )
        } else {
            (
                candidate.bbox[0]
                    .saturating_sub(bbox[2])
                    .max(bbox[0].saturating_sub(candidate.bbox[2])),
                candidate.bbox[1]
                    .saturating_sub(bbox[3])
                    .max(bbox[1].saturating_sub(candidate.bbox[3])),
            )
        };
        if perpendicular_distance > 0 {
            continue;
        }
        let candidate_length = if horizontal {
            candidate.bbox[2] - candidate.bbox[0]
        } else {
            candidate.bbox[3] - candidate.bbox[1]
        };
        let bbox_length = if horizontal {
            bbox[2] - bbox[0]
        } else {
            bbox[3] - bbox[1]
        };
        if candidate_length.min(bbox_length) as f64
            / (candidate_length.max(bbox_length).max(1) as f64)
            < 0.25
        {
            continue;
        }
        aligned += 1;
        let axial_tolerance = (2 * minimum_rule_length).max(4 * bbox_length);
        if axial_distance <= axial_tolerance {
            return true;
        }
    }
    aligned >= 2
}

pub fn thin_line_axis(
    bbox: [usize; 4],
    pixels: usize,
    structural_bands: &[RuleDraftSummary],
    maximum_rule_thickness: usize,
    minimum_rule_length: usize,
) -> Option<Option<bool>> {
    if bbox[0] >= bbox[2] || bbox[1] >= bbox[3] {
        return None;
    }
    let area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]);
    if pixels == 0 || pixels as f64 / (area as f64) < 0.7 {
        return Some(None);
    }
    for (horizontal, thickness, length) in [
        (true, bbox[3] - bbox[1], bbox[2] - bbox[0]),
        (false, bbox[2] - bbox[0], bbox[3] - bbox[1]),
    ] {
        if thickness > maximum_rule_thickness {
            continue;
        }
        let aspect_ratio = length as f64 / thickness.max(1) as f64;
        if length >= 8_usize.max(3 * thickness)
            && aspect_ratio >= 4.0
            && near_collinear_rule(horizontal, bbox, structural_bands, minimum_rule_length)
        {
            return Some(Some(horizontal));
        }
    }
    Some(None)
}

pub fn unsupported_horizontal_line_component(
    component_index: usize,
    non_rule_foreground: &[u8],
    width: usize,
    height: usize,
    components: &[ConnectedComponent],
    body_height: f64,
    maximum_rule_thickness: usize,
    minimum_rule_length: usize,
    minimum_rule_aspect_ratio: f64,
) -> Option<bool> {
    if width == 0
        || height == 0
        || non_rule_foreground.len() != width.checked_mul(height)?
        || !body_height.is_finite()
        || body_height < 0.0
        || !minimum_rule_aspect_ratio.is_finite()
        || minimum_rule_aspect_ratio < 0.0
    {
        return None;
    }
    let component = components.get(component_index)?;
    let bbox = component.bbox;
    let bbox_width = bbox[2] - bbox[0];
    let bbox_height = bbox[3] - bbox[1];
    let area = bbox_width * bbox_height;
    if !(bbox_height <= maximum_rule_thickness
        && bbox_height <= 2_usize.max((0.35 * body_height).floor() as usize)
        && bbox_width >= minimum_rule_length
        && bbox_width as f64 / bbox_height as f64 >= minimum_rule_aspect_ratio
        && component.pixels as f64 / area as f64 >= 0.7)
    {
        return Some(false);
    }
    if bbox[1] > 0
        && non_rule_foreground[(bbox[1] - 1) * width + bbox[0]..(bbox[1] - 1) * width + bbox[2]]
            .iter()
            .any(|value| *value != 0)
    {
        return Some(false);
    }
    let lookaround = 8_usize.max((2.0 * body_height).round_ties_even() as usize);
    if components.iter().enumerate().any(|(index, candidate)| {
        index != component_index
            && (candidate.bbox[1] as isize) < bbox[1] as isize - lookaround as isize
            && candidate.bbox[3] > bbox[3].saturating_add(lookaround)
            && candidate.bbox[0] < bbox[2]
            && bbox[0] < candidate.bbox[2]
    }) {
        return Some(false);
    }
    let above_top = bbox[1].saturating_sub(lookaround);
    let above_pixels: usize = (above_top..bbox[1])
        .map(|row| {
            non_rule_foreground[row * width + bbox[0]..row * width + bbox[2]]
                .iter()
                .filter(|value| **value != 0)
                .count()
        })
        .sum();
    let lower_bound = bbox[1] as isize - lookaround as isize;
    let upper_components = components
        .iter()
        .enumerate()
        .filter(|(index, candidate)| {
            *index != component_index
                && lower_bound <= candidate.bbox[3] as isize
                && candidate.bbox[3] <= bbox[1]
                && candidate.bbox[0] < bbox[2]
                && bbox[0] < candidate.bbox[2]
        })
        .count();
    Some(
        upper_components >= 2
            && above_pixels >= 4_usize.max((0.05 * bbox_width as f64).round_ties_even() as usize),
    )
}

pub fn component_has_rule_evidence(
    component: ConnectedComponent,
    line_evidence: &[u8],
    width: usize,
    height: usize,
) -> Option<bool> {
    if width == 0
        || height == 0
        || line_evidence.len() != width.checked_mul(height)?
        || component.bbox[0] >= component.bbox[2]
        || component.bbox[1] >= component.bbox[3]
        || component.bbox[2] > width
        || component.bbox[3] > height
        || component.pixels == 0
    {
        return None;
    }
    let pixels: usize = (component.bbox[1]..component.bbox[3])
        .map(|row| {
            line_evidence[row * width + component.bbox[0]..row * width + component.bbox[2]]
                .iter()
                .filter(|value| **value != 0)
                .count()
        })
        .sum();
    Some(pixels as f64 / component.pixels as f64 >= 0.7)
}

pub fn pure_vertical_line_component(component: ConnectedComponent) -> Option<bool> {
    if component.bbox[0] >= component.bbox[2] || component.bbox[1] >= component.bbox[3] {
        return None;
    }
    let width = component.bbox[2] - component.bbox[0];
    let height = component.bbox[3] - component.bbox[1];
    let area = width * height;
    Some(
        width <= 2
            && height >= 8_usize.max(3 * width)
            && height as f64 / width as f64 >= 4.0
            && component.pixels as f64 / area as f64 >= 0.7,
    )
}

pub fn horizontal_network_supported(
    component: ConnectedComponent,
    structural_bands: &[RuleDraftSummary],
    maximum_rule_thickness: usize,
) -> Option<bool> {
    if component.bbox[0] >= component.bbox[2] || component.bbox[1] >= component.bbox[3] {
        return None;
    }
    let bbox = component.bbox;
    let width = bbox[2] - bbox[0];
    let height = bbox[3] - bbox[1];
    let area = width * height;
    if !(height <= maximum_rule_thickness
        && width >= 8_usize.max(3 * height)
        && width as f64 / height as f64 >= 4.0
        && component.pixels as f64 / area as f64 >= 0.7)
    {
        return Some(false);
    }
    let tolerance = maximum_rule_thickness + 1;
    let crossings = structural_bands
        .iter()
        .filter(|candidate| {
            !candidate.horizontal
                && (candidate.bbox[1] as isize - tolerance as isize) <= bbox[1] as isize
                && bbox[3] <= candidate.bbox[3].saturating_add(tolerance)
                && (bbox[0] as isize - tolerance as isize) < candidate.bbox[2] as isize
                && candidate.bbox[0] < bbox[2].saturating_add(tolerance)
        })
        .count();
    Some(crossings >= 2)
}

pub fn vertical_network_supported(
    component_index: usize,
    components: &[ConnectedComponent],
    structural_bands: &[RuleDraftSummary],
    maximum_rule_thickness: usize,
    minimum_rule_length: usize,
) -> Option<bool> {
    let component = *components.get(component_index)?;
    if !pure_vertical_line_component(component)? {
        return Some(false);
    }
    let bbox = component.bbox;
    if near_collinear_rule(false, bbox, structural_bands, minimum_rule_length) {
        return Some(true);
    }
    let tolerance = maximum_rule_thickness + 1;
    let horizontal: Vec<&RuleDraftSummary> = structural_bands
        .iter()
        .filter(|candidate| {
            candidate.horizontal && candidate.bbox[0] < bbox[2] && bbox[0] < candidate.bbox[2]
        })
        .collect();
    let touches = |endpoint: usize| {
        horizontal.iter().any(|candidate| {
            candidate.bbox[1]
                .saturating_sub(endpoint)
                .max(endpoint.saturating_sub(candidate.bbox[3]))
                <= tolerance
        })
    };
    let endpoint_crossings = usize::from(touches(bbox[1])) + usize::from(touches(bbox[3]));
    if endpoint_crossings >= 2 {
        return Some(true);
    }
    if endpoint_crossings == 0 {
        return Some(false);
    }
    let peers: Vec<ConnectedComponent> = components
        .iter()
        .enumerate()
        .filter(|(index, _)| *index != component_index)
        .filter_map(|(_, candidate)| {
            pure_vertical_line_component(*candidate)
                .unwrap_or(false)
                .then_some(*candidate)
        })
        .collect();
    let repeated_column = peers.iter().any(|candidate| {
        candidate.bbox[0]
            .saturating_sub(bbox[2])
            .max(bbox[0].saturating_sub(candidate.bbox[2]))
            <= tolerance
    });
    let row_peers: Vec<ConnectedComponent> = peers
        .iter()
        .copied()
        .filter(|candidate| {
            candidate.bbox[1].abs_diff(bbox[1]) <= tolerance
                && candidate.bbox[3].abs_diff(bbox[3]) <= tolerance
        })
        .collect();
    let row_count = 1 + row_peers.len();
    let row_left = row_peers
        .iter()
        .map(|candidate| candidate.bbox[0])
        .fold(bbox[0], usize::min);
    let row_right = row_peers
        .iter()
        .map(|candidate| candidate.bbox[2])
        .fold(bbox[2], usize::max);
    let repeated_row = row_count >= 3
        && row_right - row_left >= (2 * minimum_rule_length).max(4 * (bbox[3] - bbox[1]));
    Some(repeated_column || repeated_row)
}

pub fn recover_residual_line_drafts(
    non_rule_foreground: &[u8],
    line_evidence: &[u8],
    width: usize,
    height: usize,
    rules: &[RuleDraftSummary],
    components: &[ConnectedComponent],
    maximum_rule_thickness: usize,
    minimum_rule_length: usize,
    minimum_rule_aspect_ratio: f64,
) -> Option<Vec<RecoveredRuleDraft>> {
    if width == 0
        || height == 0
        || non_rule_foreground.len() != width.checked_mul(height)?
        || line_evidence.len() != non_rule_foreground.len()
    {
        return None;
    }
    let structural_bands: Vec<RuleDraftSummary> = rules
        .iter()
        .copied()
        .filter(|rule| {
            !((rule.horizontal && (rule.bbox[1] == 0 || rule.bbox[3] == height))
                || (!rule.horizontal && (rule.bbox[0] == 0 || rule.bbox[2] == width)))
        })
        .collect();
    let body_height = body_component_height(components, 50.0);
    let mut values = Vec::new();
    let mut seen = std::collections::HashSet::<(bool, [usize; 4])>::new();
    for (component_index, component) in components.iter().copied().enumerate() {
        let mut axis = thin_line_axis(
            component.bbox,
            component.pixels,
            &structural_bands,
            maximum_rule_thickness,
            minimum_rule_length,
        )?;
        if axis.is_some() && !component_has_rule_evidence(component, line_evidence, width, height)?
        {
            axis = None;
        }
        if axis.is_none()
            && component_has_rule_evidence(component, line_evidence, width, height)?
            && horizontal_network_supported(component, &structural_bands, maximum_rule_thickness)?
        {
            axis = Some(true);
        }
        if axis.is_none()
            && unsupported_horizontal_line_component(
                component_index,
                non_rule_foreground,
                width,
                height,
                components,
                body_height,
                maximum_rule_thickness,
                minimum_rule_length,
                minimum_rule_aspect_ratio,
            )?
        {
            axis = Some(true);
        }
        if axis.is_none()
            && vertical_network_supported(
                component_index,
                components,
                &structural_bands,
                maximum_rule_thickness,
                minimum_rule_length,
            )?
        {
            axis = Some(false);
        }
        let Some(horizontal) = axis else {
            continue;
        };
        if !seen.insert((horizontal, component.bbox)) {
            continue;
        }
        values.push(RecoveredRuleDraft {
            summary: RuleDraftSummary {
                horizontal,
                bbox: component.bbox,
            },
            claim_full_bbox: false,
            partition_evidence: true,
        });
    }
    Some(values)
}

pub fn recover_leaf_rule_networks(
    rule_evidence: &[u8],
    unclaimed_foreground: &[u8],
    claimed_rules: &[u8],
    width: usize,
    height: usize,
    rules: &[RuleDraftSummary],
    nodes: &[PartitionNode],
    components: &[ConnectedComponent],
    runs: &[ConnectedComponentRun],
    maximum_rule_thickness: usize,
    minimum_rule_length: usize,
    minimum_rule_aspect_ratio: f64,
) -> Option<Vec<RecoveredRuleDraft>> {
    if width == 0
        || height == 0
        || rule_evidence.len() != width.checked_mul(height)?
        || unclaimed_foreground.len() != rule_evidence.len()
        || claimed_rules.len() != rule_evidence.len()
        || nodes.is_empty()
    {
        return None;
    }
    let residual: Vec<u8> = rule_evidence
        .iter()
        .zip(claimed_rules)
        .map(|(evidence, claimed)| u8::from(*evidence != 0 && *claimed == 0))
        .collect();
    let long_horizontal_evidence =
        orientation_candidates(&residual, width, height, true, minimum_rule_length)?;
    let leaves: Vec<usize> = nodes
        .iter()
        .enumerate()
        .filter(|(_, node)| node.child_indexes == [None, None])
        .map(|(index, _)| index)
        .collect();
    let mut component_runs = vec![Vec::<ConnectedComponentRun>::new(); components.len()];
    for run in runs {
        component_runs.get_mut(run.component_index)?.push(*run);
    }
    let leaf_for_run = |run: ConnectedComponentRun| -> Option<usize> {
        let mut node_index = 0_usize;
        loop {
            let node = nodes.get(node_index)?;
            let [Some(first), Some(second)] = node.child_indexes else {
                return (node.child_indexes == [None, None]).then_some(node_index);
            };
            let matching: Vec<usize> = [first, second]
                .into_iter()
                .filter(|child_index| {
                    let child = &nodes[*child_index];
                    child.bbox[0] <= run.start
                        && run.start < child.bbox[2]
                        && child.bbox[1] <= run.row
                        && run.row < child.bbox[3]
                        && child.bbox[0] <= run.stop - 1
                        && run.stop - 1 < child.bbox[2]
                })
                .collect();
            if matching.len() != 1 {
                return None;
            }
            node_index = matching[0];
        }
    };
    let mut components_by_leaf = std::collections::HashMap::<usize, Vec<usize>>::new();
    for leaf in &leaves {
        components_by_leaf.insert(*leaf, Vec::new());
    }
    for component_index in 0..components.len() {
        let first_run = *component_runs.get(component_index)?.first()?;
        let leaf_index = leaf_for_run(first_run)?;
        components_by_leaf
            .get_mut(&leaf_index)?
            .push(component_index);
    }
    let maximum_thickness =
        maximum_rule_thickness.max((width.min(height) as f64 * 0.003).round_ties_even() as usize);
    let mut recovered = Vec::new();
    let mut seen = std::collections::HashSet::<(bool, [usize; 4])>::new();
    let mut leaf_contents = std::collections::HashMap::<usize, [usize; 4]>::new();
    let mut networks = Vec::<(usize, [usize; 4], Vec<(usize, usize)>)>::new();

    let true_runs = |values: &[bool]| {
        let mut output = Vec::new();
        let mut start = 0;
        while start < values.len() {
            if !values[start] {
                start += 1;
                continue;
            }
            let mut stop = start + 1;
            while stop < values.len() && values[stop] {
                stop += 1;
            }
            output.push((start, stop));
            start = stop;
        }
        output
    };

    for leaf_index in &leaves {
        let local_indexes = components_by_leaf.get(leaf_index)?;
        if local_indexes.is_empty() {
            continue;
        }
        let content = [
            local_indexes
                .iter()
                .map(|index| components[*index].bbox[0])
                .min()?,
            local_indexes
                .iter()
                .map(|index| components[*index].bbox[1])
                .min()?,
            local_indexes
                .iter()
                .map(|index| components[*index].bbox[2])
                .max()?,
            local_indexes
                .iter()
                .map(|index| components[*index].bbox[3])
                .max()?,
        ];
        leaf_contents.insert(*leaf_index, content);
        let content_width = content[2] - content[0];
        let content_height = content[3] - content[1];
        if content_width < 2 * minimum_rule_length
            || content_height < 8_usize.max(minimum_rule_length / 3)
        {
            continue;
        }
        let dense_rows: Vec<bool> = (content[1]..content[3])
            .map(|row| {
                long_horizontal_evidence[row * width + content[0]..row * width + content[2]]
                    .iter()
                    .filter(|value| **value != 0)
                    .count()
                    >= minimum_rule_length.max((content_width as f64 * 0.75).ceil() as usize)
            })
            .collect();
        let dense_columns: Vec<bool> = (content[0]..content[2])
            .map(|column| {
                (content[1]..content[3])
                    .filter(|row| residual[row * width + column] != 0)
                    .count()
                    >= 4_usize.max((content_height as f64 * 0.55).ceil() as usize)
            })
            .collect();
        let row_bands: Vec<(usize, usize)> = true_runs(&dense_rows)
            .into_iter()
            .filter(|(start, stop)| stop - start <= maximum_thickness)
            .collect();
        let column_bands: Vec<(usize, usize)> = true_runs(&dense_columns)
            .into_iter()
            .filter(|(start, stop)| {
                stop - start <= maximum_thickness && *start > 0 && *stop < content_width
            })
            .collect();
        if column_bands.len() < 3 {
            continue;
        }
        let crossed_rows: Vec<(usize, usize)> = row_bands
            .iter()
            .copied()
            .filter(|(row_start, row_stop)| {
                column_bands
                    .iter()
                    .filter(|(left, right)| {
                        (*row_start..*row_stop).any(|row| {
                            residual[(content[1] + row) * width + content[0] + left
                                ..(content[1] + row) * width + content[0] + right]
                                .iter()
                                .any(|value| *value != 0)
                        })
                    })
                    .count()
                    >= 3
            })
            .collect();
        if crossed_rows.is_empty() {
            let guided_bands: Vec<(usize, usize)> = column_bands
                .iter()
                .copied()
                .filter(|(start, stop)| {
                    (content[1]..content[3])
                        .filter(|row| {
                            residual[*row * width + content[0] + start
                                ..*row * width + content[0] + stop]
                                .iter()
                                .any(|value| *value != 0)
                        })
                        .count()
                        >= (content_height as f64 * 0.8).ceil() as usize
                })
                .filter(|(start, stop)| {
                    supports_projected_column_gap(
                        rules,
                        content,
                        content[0] + start,
                        content[0] + stop,
                        content_height as f64,
                    )
                    .unwrap_or(false)
                })
                .collect();
            if guided_bands.len() < 16 {
                continue;
            }
            for (start, stop) in guided_bands {
                let bbox = [
                    content[0] + start,
                    content[1],
                    content[0] + stop,
                    content[3],
                ];
                if seen.insert((false, bbox)) {
                    recovered.push(RecoveredRuleDraft {
                        summary: RuleDraftSummary {
                            horizontal: false,
                            bbox,
                        },
                        claim_full_bbox: false,
                        partition_evidence: true,
                    });
                }
            }
            continue;
        }
        let global_column_bands: Vec<(usize, usize)> = column_bands
            .iter()
            .map(|(start, stop)| (content[0] + start, content[0] + stop))
            .collect();
        networks.push((*leaf_index, content, global_column_bands));
        for (start, stop) in &crossed_rows {
            let bbox = [
                content[0],
                content[1] + start,
                content[2],
                content[1] + stop,
            ];
            if seen.insert((true, bbox)) {
                recovered.push(RecoveredRuleDraft {
                    summary: RuleDraftSummary {
                        horizontal: true,
                        bbox,
                    },
                    claim_full_bbox: true,
                    partition_evidence: true,
                });
            }
        }
        for (start, stop) in &column_bands {
            let crosses = crossed_rows.iter().any(|(row_start, row_stop)| {
                (*row_start..*row_stop).any(|row| {
                    residual[(content[1] + row) * width + content[0] + start
                        ..(content[1] + row) * width + content[0] + stop]
                        .iter()
                        .any(|value| *value != 0)
                })
            });
            if !crosses {
                continue;
            }
            let bbox = [
                content[0] + start,
                content[1],
                content[0] + stop,
                content[3],
            ];
            if seen.insert((false, bbox)) {
                recovered.push(RecoveredRuleDraft {
                    summary: RuleDraftSummary {
                        horizontal: false,
                        bbox,
                    },
                    claim_full_bbox: false,
                    partition_evidence: true,
                });
            }
        }
    }

    for leaf_index in &leaves {
        let Some(content) = leaf_contents.get(leaf_index).copied() else {
            continue;
        };
        if networks.iter().any(|(source, _, _)| source == leaf_index) {
            continue;
        }
        let mut candidates = Vec::new();
        for (source_index, source, bands) in &networks {
            if source_index == leaf_index {
                continue;
            }
            let vertical_distance = source[1]
                .saturating_sub(content[3])
                .max(content[1].saturating_sub(source[3]));
            if vertical_distance > (2 * minimum_rule_length).max(2 * (content[3] - content[1])) {
                continue;
            }
            let overlapping: Vec<(usize, usize)> = bands
                .iter()
                .copied()
                .filter(|(left, right)| content[0] < *left && *right < content[2])
                .collect();
            if overlapping.len() >= 3 {
                candidates.push((vertical_distance, *source, overlapping));
            }
        }
        let Some((_, _, projected_bands)) = candidates.into_iter().min_by(|first, second| {
            first
                .0
                .cmp(&second.0)
                .then_with(|| second.2.len().cmp(&first.2.len()))
                .then_with(|| first.1[1].cmp(&second.1[1]))
        }) else {
            continue;
        };
        let mut supported = Vec::new();
        for (left, right) in projected_bands {
            let trace: Vec<bool> = (content[1]..content[3])
                .map(|row| {
                    residual[row * width + left..row * width + right]
                        .iter()
                        .any(|value| *value != 0)
                })
                .collect();
            let longest = true_runs(&trace)
                .into_iter()
                .map(|(start, stop)| stop - start)
                .max()
                .unwrap_or(0);
            if longest >= 4_usize.max(((content[3] - content[1]) as f64 * 0.7).ceil() as usize) {
                supported.push((left, right));
            }
        }
        if supported.len() < 8 {
            continue;
        }
        for (left, right) in supported {
            let bbox = [left, content[1], right, content[3]];
            if seen.insert((false, bbox)) {
                recovered.push(RecoveredRuleDraft {
                    summary: RuleDraftSummary {
                        horizontal: false,
                        bbox,
                    },
                    claim_full_bbox: false,
                    partition_evidence: true,
                });
            }
        }
    }

    let mut structural_bands: Vec<RuleDraftSummary> = rules
        .iter()
        .copied()
        .filter(|rule| {
            !((rule.horizontal && (rule.bbox[1] == 0 || rule.bbox[3] == height))
                || (!rule.horizontal && (rule.bbox[0] == 0 || rule.bbox[2] == width)))
        })
        .collect();
    structural_bands.extend(recovered.iter().map(|draft| draft.summary));
    let body_height = body_component_height(components, 50.0);
    for (component_index, component) in components.iter().copied().enumerate() {
        let mut axis = thin_line_axis(
            component.bbox,
            component.pixels,
            &structural_bands,
            maximum_rule_thickness,
            minimum_rule_length,
        )?;
        if axis.is_some() && !component_has_rule_evidence(component, &residual, width, height)? {
            axis = None;
        }
        if axis.is_none()
            && component_has_rule_evidence(component, &residual, width, height)?
            && horizontal_network_supported(component, &structural_bands, maximum_rule_thickness)?
        {
            axis = Some(true);
        }
        if axis.is_none()
            && unsupported_horizontal_line_component(
                component_index,
                unclaimed_foreground,
                width,
                height,
                components,
                body_height,
                maximum_rule_thickness,
                minimum_rule_length,
                minimum_rule_aspect_ratio,
            )?
        {
            if seen.insert((true, component.bbox)) {
                recovered.push(RecoveredRuleDraft {
                    summary: RuleDraftSummary {
                        horizontal: true,
                        bbox: component.bbox,
                    },
                    claim_full_bbox: false,
                    partition_evidence: false,
                });
            }
            continue;
        }
        if axis.is_none()
            && vertical_network_supported(
                component_index,
                components,
                &structural_bands,
                maximum_rule_thickness,
                minimum_rule_length,
            )?
        {
            axis = Some(false);
        }
        let Some(horizontal) = axis else {
            continue;
        };
        if seen.insert((horizontal, component.bbox)) {
            recovered.push(RecoveredRuleDraft {
                summary: RuleDraftSummary {
                    horizontal,
                    bbox: component.bbox,
                },
                claim_full_bbox: false,
                partition_evidence: true,
            });
        }
    }
    Some(recovered)
}

pub fn best_component_rule_boundary(
    components: &[ConnectedComponent],
    bbox: [usize; 4],
    rules: &[RuleDraftSummary],
) -> Option<Option<ComponentColumnBoundary>> {
    if bbox[0] >= bbox[2]
        || bbox[1] >= bbox[3]
        || components.iter().any(|component| {
            component.bbox[0] >= component.bbox[2] || component.bbox[1] >= component.bbox[3]
        })
    {
        return None;
    }
    if components.len() < 2 {
        return Some(None);
    }
    let content = [
        components.iter().map(|component| component.bbox[0]).min()?,
        components.iter().map(|component| component.bbox[1]).min()?,
        components.iter().map(|component| component.bbox[2]).max()?,
        components.iter().map(|component| component.bbox[3]).max()?,
    ];
    let content_height = content[3] - content[1];
    let minimum_intersection_height = content_height.saturating_mul(4).div_ceil(5);
    let mut best: Option<(f64, f64, usize, ComponentColumnBoundary)> = None;
    for rule in rules.iter().filter(|rule| !rule.horizontal) {
        let intersection_top = rule.bbox[1].max(content[1]);
        let intersection_bottom = rule.bbox[3].min(content[3]);
        if intersection_top >= intersection_bottom
            || intersection_bottom - intersection_top < minimum_intersection_height
            || rule.bbox[0] <= bbox[0]
            || rule.bbox[2] >= bbox[2]
        {
            continue;
        }
        let left_indexes: Vec<usize> = components
            .iter()
            .enumerate()
            .filter(|(_, component)| component.bbox[2] <= rule.bbox[0])
            .map(|(index, _)| index)
            .collect();
        let right_indexes: Vec<usize> = components
            .iter()
            .enumerate()
            .filter(|(_, component)| component.bbox[0] >= rule.bbox[2])
            .map(|(index, _)| index)
            .collect();
        if left_indexes.is_empty()
            || right_indexes.is_empty()
            || left_indexes.len() + right_indexes.len() != components.len()
        {
            continue;
        }
        let left_pixels: usize = left_indexes
            .iter()
            .map(|index| components[*index].pixels)
            .sum();
        let right_pixels: usize = right_indexes
            .iter()
            .map(|index| components[*index].pixels)
            .sum();
        let maximum_pixels = left_pixels.max(right_pixels);
        let pixel_balance = if maximum_pixels == 0 {
            0.0
        } else {
            left_pixels.min(right_pixels) as f64 / maximum_pixels as f64
        };
        let component_balance = left_indexes.len().min(right_indexes.len()) as f64
            / left_indexes.len().max(right_indexes.len()) as f64;
        let coverage = (intersection_bottom - intersection_top) as f64 / content_height as f64;
        let balance = pixel_balance + 0.25 * component_balance;
        let decision = ComponentColumnBoundary {
            separator: [rule.bbox[0], bbox[1], rule.bbox[2], bbox[3]],
            left_indexes,
            right_indexes,
        };
        let replace = match &best {
            None => true,
            Some((best_coverage, best_balance, best_left, _)) => coverage
                .total_cmp(best_coverage)
                .then_with(|| balance.total_cmp(best_balance))
                .then_with(|| best_left.cmp(&rule.bbox[0]))
                .is_gt(),
        };
        if replace {
            best = Some((coverage, balance, rule.bbox[0], decision));
        }
    }
    Some(best.map(|(_, _, _, decision)| decision))
}

fn source_box(transform: AffineBoxTransform, bbox: [usize; 4]) -> Option<[usize; 4]> {
    if transform.original_size[0] == 0
        || transform.original_size[1] == 0
        || transform.inverse.iter().any(|value| !value.is_finite())
    {
        return None;
    }
    let apply = |x: usize, y: usize| {
        let x = x as f64;
        let y = y as f64;
        let denominator =
            transform.inverse[6] * x + transform.inverse[7] * y + transform.inverse[8];
        if denominator.abs() < 1.0e-12 {
            return None;
        }
        Some((
            (transform.inverse[0] * x + transform.inverse[1] * y + transform.inverse[2])
                / denominator,
            (transform.inverse[3] * x + transform.inverse[4] * y + transform.inverse[5])
                / denominator,
        ))
    };
    let points = [
        apply(bbox[0], bbox[1])?,
        apply(bbox[2], bbox[1])?,
        apply(bbox[0], bbox[3])?,
        apply(bbox[2], bbox[3])?,
    ];
    let epsilon = 1.0e-7;
    let raw_left = (points
        .iter()
        .map(|point| point.0)
        .fold(f64::INFINITY, f64::min)
        + epsilon)
        .floor() as isize;
    let raw_top = (points
        .iter()
        .map(|point| point.1)
        .fold(f64::INFINITY, f64::min)
        + epsilon)
        .floor() as isize;
    let raw_right = (points
        .iter()
        .map(|point| point.0)
        .fold(f64::NEG_INFINITY, f64::max)
        - epsilon)
        .ceil() as isize;
    let raw_bottom = (points
        .iter()
        .map(|point| point.1)
        .fold(f64::NEG_INFINITY, f64::max)
        - epsilon)
        .ceil() as isize;
    let width = transform.original_size[0] as isize;
    let height = transform.original_size[1] as isize;
    let left = raw_left.clamp(0, width - 1);
    let top = raw_top.clamp(0, height - 1);
    let right = raw_right.clamp(left + 1, width);
    let bottom = raw_bottom.clamp(top + 1, height);
    Some([left as usize, top as usize, right as usize, bottom as usize])
}

pub fn materialize_segments(
    components: &[ConnectedComponent],
    component_ids: &[usize],
    runs: &[ConnectedComponentRun],
    nodes: &[PartitionNode],
    transform: AffineBoxTransform,
    width: usize,
    height: usize,
) -> Option<MaterializedSegments> {
    if nodes.is_empty() || components.len() != component_ids.len() || width == 0 || height == 0 {
        return None;
    }
    let mut component_runs = vec![Vec::<ConnectedComponentRun>::new(); components.len()];
    for run in runs {
        if run.row >= height || run.start >= run.stop || run.stop > width {
            return None;
        }
        component_runs.get_mut(run.component_index)?.push(*run);
    }
    if component_runs.iter().any(Vec::is_empty) {
        return None;
    }
    let leaves: Vec<usize> = nodes
        .iter()
        .enumerate()
        .filter(|(_, node)| node.child_indexes == [None, None])
        .map(|(index, _)| index)
        .collect();
    let contains_point = |bbox: [usize; 4], x: usize, y: usize| {
        bbox[0] <= x && x < bbox[2] && bbox[1] <= y && y < bbox[3]
    };
    let mut grouped = vec![Vec::<usize>::new(); nodes.len()];
    for (component_index, local_runs) in component_runs.iter().enumerate() {
        let first_run = local_runs[0];
        let mut node_index = 0_usize;
        loop {
            let node = nodes.get(node_index)?;
            match node.child_indexes {
                [None, None] => break,
                [Some(first), Some(second)] => {
                    let first_contains =
                        contains_point(nodes.get(first)?.bbox, first_run.start, first_run.row);
                    let second_contains =
                        contains_point(nodes.get(second)?.bbox, first_run.start, first_run.row);
                    node_index = match (first_contains, second_contains) {
                        (true, false) => first,
                        (false, true) => second,
                        _ => return None,
                    };
                }
                _ => return None,
            }
        }
        grouped.get_mut(node_index)?.push(component_index);
    }
    let mut drafts = Vec::<([usize; 4], usize, Vec<usize>)>::new();
    for leaf_index in leaves {
        let values = std::mem::take(grouped.get_mut(leaf_index)?);
        if values.is_empty() {
            continue;
        }
        let bbox = [
            values
                .iter()
                .map(|index| components[*index].bbox[0])
                .min()?,
            values
                .iter()
                .map(|index| components[*index].bbox[1])
                .min()?,
            values
                .iter()
                .map(|index| components[*index].bbox[2])
                .max()?,
            values
                .iter()
                .map(|index| components[*index].bbox[3])
                .max()?,
        ];
        drafts.push((bbox, leaf_index, values));
    }
    drafts.sort_by_key(|(bbox, _, _)| (bbox[1], bbox[0], bbox[3], bbox[2]));
    let mut row_tops: Vec<usize> = drafts.iter().map(|(bbox, _, _)| bbox[1]).collect();
    row_tops.sort_unstable();
    row_tops.dedup();
    let mut ownership = vec![-1_isize; width.checked_mul(height)?];
    let mut segments = Vec::<MaterializedSegment>::with_capacity(drafts.len());
    let mut leaf_segment_indexes = vec![None; nodes.len()];
    for (segment_index, (bbox, leaf_index, values)) in drafts.into_iter().enumerate() {
        let source_bbox = source_box(transform, bbox)?;
        let ink_pixels = values.iter().map(|index| components[*index].pixels).sum();
        let row_index = row_tops.binary_search(&bbox[1]).ok()?;
        let ids = values.iter().map(|index| component_ids[*index]).collect();
        segments.push(MaterializedSegment {
            bbox,
            source_bbox,
            ink_pixels,
            row_index,
            leaf_index,
            component_ids: ids,
        });
        leaf_segment_indexes[leaf_index] = Some(segment_index);
        for component_index in values {
            for run in &component_runs[component_index] {
                let start = run.row.checked_mul(width)?.checked_add(run.start)?;
                let stop = run.row.checked_mul(width)?.checked_add(run.stop)?;
                if ownership[start..stop].iter().any(|owner| *owner != -1) {
                    return None;
                }
                ownership[start..stop].fill(segment_index as isize);
            }
        }
    }
    Some(MaterializedSegments {
        segments,
        ownership,
        leaf_segment_indexes,
    })
}

pub fn materialize_nodes(
    nodes: &[PartitionNode],
    leaf_segment_indexes: &[Option<usize>],
    segments: &[MaterializedSegment],
) -> Option<Vec<MaterializedNode>> {
    if nodes.is_empty() || nodes.len() != leaf_segment_indexes.len() {
        return None;
    }
    let mut descendants = vec![Vec::<usize>::new(); nodes.len()];
    for node_index in (0..nodes.len()).rev() {
        match nodes[node_index].child_indexes {
            [Some(first), Some(second)] => {
                let mut values = descendants.get(first)?.clone();
                values.extend(descendants.get(second)?.iter().copied());
                if values.iter().any(|index| *index >= segments.len()) {
                    return None;
                }
                values.sort_by_key(|index| {
                    let bbox = segments[*index].bbox;
                    (bbox[1], bbox[0])
                });
                descendants[node_index] = values;
            }
            [None, None] => {
                if let Some(segment_index) = leaf_segment_indexes[node_index] {
                    if segment_index >= segments.len() {
                        return None;
                    }
                    descendants[node_index].push(segment_index);
                }
            }
            _ => return None,
        }
    }
    let mut preorder = Vec::<MaterializedNode>::with_capacity(nodes.len());
    let mut stack = vec![0_usize];
    let mut seen = vec![false; nodes.len()];
    while let Some(node_index) = stack.pop() {
        if *seen.get(node_index)? {
            return None;
        }
        seen[node_index] = true;
        preorder.push(MaterializedNode {
            source_index: node_index,
            segment_indexes: descendants[node_index].clone(),
        });
        match nodes[node_index].child_indexes {
            [Some(first), Some(second)] => {
                stack.push(second);
                stack.push(first);
            }
            [None, None] => {}
            _ => return None,
        }
    }
    if seen.iter().any(|value| !value) {
        return None;
    }
    Some(preorder)
}

pub fn project_sparse_matrix(
    width: usize,
    height: usize,
    ownership: &[isize],
    segment_count: usize,
    rules: &[RuleDraftSummary],
) -> Option<MaterializedSparseMatrix> {
    if width == 0 || height == 0 || ownership.len() != width.checked_mul(height)? {
        return None;
    }
    if segment_count == 0 && rules.is_empty() {
        return Some(MaterializedSparseMatrix {
            rows: Vec::new(),
            columns: Vec::new(),
            cells: Vec::new(),
            spans: Vec::new(),
            horizontal_rule_rows: Vec::new(),
            vertical_rule_columns: Vec::new(),
        });
    }
    let mut row_cuts = std::collections::BTreeSet::from([0_usize, height]);
    let mut column_cuts = std::collections::BTreeSet::from([0_usize, width]);
    for rule in rules {
        if rule.bbox[0] >= rule.bbox[2]
            || rule.bbox[1] >= rule.bbox[3]
            || rule.bbox[2] > width
            || rule.bbox[3] > height
        {
            return None;
        }
        if rule.horizontal {
            row_cuts.insert(rule.bbox[1]);
            row_cuts.insert(rule.bbox[3]);
        } else {
            column_cuts.insert(rule.bbox[0]);
            column_cuts.insert(rule.bbox[2]);
        }
    }
    let intervals = |cuts: std::collections::BTreeSet<usize>| {
        let ordered: Vec<usize> = cuts.into_iter().collect();
        ordered
            .windows(2)
            .filter(|pair| pair[1] > pair[0])
            .map(|pair| SparseAxisInterval {
                start: pair[0],
                end: pair[1],
            })
            .collect::<Vec<_>>()
    };
    let rows = intervals(row_cuts);
    let columns = intervals(column_cuts);
    let mut row_lookup = vec![0_usize; height];
    let mut column_lookup = vec![0_usize; width];
    for (index, interval) in rows.iter().enumerate() {
        row_lookup[interval.start..interval.end].fill(index);
    }
    for (index, interval) in columns.iter().enumerate() {
        column_lookup[interval.start..interval.end].fill(index);
    }
    let mut cell_entries = std::collections::BTreeSet::<MaterializedSparseCell>::new();
    for pixel_row in 0..height {
        let row_offset = pixel_row * width;
        for pixel_column in 0..width {
            let owner = ownership[row_offset + pixel_column];
            if owner < 0 {
                continue;
            }
            let segment_index = owner as usize;
            if segment_index >= segment_count {
                return None;
            }
            cell_entries.insert(MaterializedSparseCell {
                row: row_lookup[pixel_row],
                column: column_lookup[pixel_column],
                segment_index,
            });
        }
    }
    let cells: Vec<MaterializedSparseCell> = cell_entries.into_iter().collect();
    let mut cells_by_segment = vec![Vec::<MaterializedSparseCell>::new(); segment_count];
    for cell in &cells {
        cells_by_segment[cell.segment_index].push(*cell);
    }
    let mut spans = Vec::<MaterializedSegmentSpan>::with_capacity(segment_count);
    for (segment_index, owned_cells) in cells_by_segment.into_iter().enumerate() {
        if owned_cells.is_empty() {
            return None;
        }
        spans.push(MaterializedSegmentSpan {
            segment_index,
            row_start: owned_cells.iter().map(|cell| cell.row).min()?,
            row_stop: owned_cells.iter().map(|cell| cell.row).max()? + 1,
            column_start: owned_cells.iter().map(|cell| cell.column).min()?,
            column_stop: owned_cells.iter().map(|cell| cell.column).max()? + 1,
        });
    }
    let overlapping = |intervals: &[SparseAxisInterval], start: usize, stop: usize| {
        intervals
            .iter()
            .enumerate()
            .filter(|(_, interval)| interval.start < stop && start < interval.end)
            .map(|(index, _)| index)
            .collect::<Vec<_>>()
    };
    let mut horizontal_rule_rows = std::collections::BTreeSet::<usize>::new();
    let mut vertical_rule_columns = std::collections::BTreeSet::<usize>::new();
    for rule in rules {
        if rule.horizontal {
            horizontal_rule_rows.extend(overlapping(&rows, rule.bbox[1], rule.bbox[3]));
        } else {
            vertical_rule_columns.extend(overlapping(&columns, rule.bbox[0], rule.bbox[2]));
        }
    }
    Some(MaterializedSparseMatrix {
        rows,
        columns,
        cells,
        spans,
        horizontal_rule_rows: horizontal_rule_rows.into_iter().collect(),
        vertical_rule_columns: vertical_rule_columns.into_iter().collect(),
    })
}

pub fn refine_component_boundaries(
    source_nodes: &[PartitionNode],
    components: &[ConnectedComponent],
    component_ids: &[usize],
    runs: &[ConnectedComponentRun],
    diacritic_guard: &[u8],
    width: usize,
    height: usize,
    config: PartitionConfig,
    rules: &[RuleDraftSummary],
) -> Option<Vec<PartitionNode>> {
    if source_nodes.is_empty()
        || components.len() != component_ids.len()
        || width == 0
        || height == 0
        || diacritic_guard.len() != width.checked_mul(height)?
    {
        return None;
    }
    let mut component_runs = vec![Vec::<ConnectedComponentRun>::new(); components.len()];
    for run in runs {
        component_runs.get_mut(run.component_index)?.push(*run);
    }
    if component_runs.iter().any(Vec::is_empty) {
        return None;
    }
    let mut nodes = source_nodes.to_vec();
    let leaves: Vec<usize> = nodes
        .iter()
        .enumerate()
        .filter(|(_, node)| node.child_indexes == [None, None])
        .map(|(index, _)| index)
        .collect();
    let mut components_by_leaf = vec![Vec::<usize>::new(); nodes.len()];
    let contains_point = |bbox: [usize; 4], x: usize, y: usize| {
        bbox[0] <= x && x < bbox[2] && bbox[1] <= y && y < bbox[3]
    };
    for (component_index, runs) in component_runs.iter().enumerate() {
        let first_run = runs[0];
        let mut node_index = 0_usize;
        loop {
            let node = nodes.get(node_index)?;
            match node.child_indexes {
                [None, None] => break,
                [Some(first), Some(second)] => {
                    let first_contains =
                        contains_point(nodes.get(first)?.bbox, first_run.start, first_run.row);
                    let second_contains =
                        contains_point(nodes.get(second)?.bbox, first_run.start, first_run.row);
                    node_index = match (first_contains, second_contains) {
                        (true, false) => first,
                        (false, true) => second,
                        _ => return None,
                    };
                }
                _ => return None,
            }
        }
        components_by_leaf
            .get_mut(node_index)?
            .push(component_index);
    }
    let mut stack: Vec<(usize, Vec<usize>)> = leaves
        .into_iter()
        .rev()
        .map(|leaf| (leaf, std::mem::take(&mut components_by_leaf[leaf])))
        .collect();
    while let Some((node_index, local_indexes)) = stack.pop() {
        let local_components: Vec<ConnectedComponent> = local_indexes
            .iter()
            .map(|index| components[*index])
            .collect();
        let local_ids: Vec<usize> = local_indexes
            .iter()
            .map(|index| component_ids[*index])
            .collect();
        let mut local_runs = Vec::<ConnectedComponentRun>::new();
        for (local_index, source_index) in local_indexes.iter().enumerate() {
            local_runs.extend(component_runs[*source_index].iter().map(|run| {
                ConnectedComponentRun {
                    component_index: local_index,
                    row: run.row,
                    start: run.start,
                    stop: run.stop,
                }
            }));
        }
        let node = nodes.get(node_index)?.clone();
        let row_decision = best_component_boundary(
            &local_components,
            &local_ids,
            &local_runs,
            node.bbox,
            diacritic_guard,
            width,
            height,
        )?;
        let column_decision = if row_decision.is_none() {
            best_component_rule_boundary(&local_components, node.bbox, rules)?
        } else {
            None
        };
        if row_decision.is_none() && column_decision.is_none() {
            continue;
        }
        if node.depth >= config.max_depth || nodes.len() + 2 > config.max_nodes {
            nodes[node_index].stop_reason = Some(PartitionStopReason::Limit);
            continue;
        }
        let (rows, separator, split_coordinate, child_boxes, first_local, second_local) =
            if let Some(decision) = row_decision {
                (
                    true,
                    None,
                    Some(decision.coordinate),
                    [
                        [
                            node.bbox[0],
                            node.bbox[1],
                            node.bbox[2],
                            decision.coordinate,
                        ],
                        [
                            node.bbox[0],
                            decision.coordinate,
                            node.bbox[2],
                            node.bbox[3],
                        ],
                    ],
                    decision.upper_indexes,
                    decision.lower_indexes,
                )
            } else {
                let decision = column_decision?;
                (
                    false,
                    Some(decision.separator),
                    None,
                    [
                        [
                            node.bbox[0],
                            node.bbox[1],
                            decision.separator[0],
                            node.bbox[3],
                        ],
                        [
                            decision.separator[2],
                            node.bbox[1],
                            node.bbox[2],
                            node.bbox[3],
                        ],
                    ],
                    decision.left_indexes,
                    decision.right_indexes,
                )
            };
        let first_components: Vec<usize> = first_local
            .into_iter()
            .map(|index| local_indexes[index])
            .collect();
        let second_components: Vec<usize> = second_local
            .into_iter()
            .map(|index| local_indexes[index])
            .collect();
        let first_child = nodes.len();
        let second_child = first_child + 1;
        nodes.push(PartitionNode {
            bbox: child_boxes[0],
            depth: node.depth + 1,
            parent_index: Some(node_index),
            rows: None,
            child_indexes: [None, None],
            separator: None,
            split_coordinate: None,
            stop_reason: Some(PartitionStopReason::Atomic),
            rule_partition: false,
            row_rule_top: false,
            row_rule_bottom: false,
        });
        nodes.push(PartitionNode {
            bbox: child_boxes[1],
            depth: node.depth + 1,
            parent_index: Some(node_index),
            rows: None,
            child_indexes: [None, None],
            separator: None,
            split_coordinate: None,
            stop_reason: Some(PartitionStopReason::Atomic),
            rule_partition: false,
            row_rule_top: false,
            row_rule_bottom: false,
        });
        nodes[node_index].rows = Some(rows);
        nodes[node_index].child_indexes = [Some(first_child), Some(second_child)];
        nodes[node_index].separator = separator;
        nodes[node_index].split_coordinate = split_coordinate;
        nodes[node_index].stop_reason = None;
        stack.push((second_child, second_components));
        stack.push((first_child, first_components));
    }
    Some(nodes)
}

pub fn best_component_boundary(
    components: &[ConnectedComponent],
    component_ids: &[usize],
    runs: &[ConnectedComponentRun],
    bbox: [usize; 4],
    diacritic_guard: &[u8],
    width: usize,
    height: usize,
) -> Option<Option<ComponentRowBoundary>> {
    if components.len() != component_ids.len()
        || width == 0
        || height == 0
        || diacritic_guard.len() != width.checked_mul(height)?
        || bbox[0] >= bbox[2]
        || bbox[1] >= bbox[3]
        || bbox[2] > width
        || bbox[3] > height
    {
        return None;
    }
    if components.len() < 2 {
        return Some(None);
    }
    let mut component_runs = vec![Vec::<ConnectedComponentRun>::new(); components.len()];
    for run in runs {
        component_runs.get_mut(run.component_index)?.push(*run);
    }
    let mut ordered: Vec<usize> = components
        .iter()
        .enumerate()
        .filter(|(_, component)| {
            component.pixels >= 4 && component.bbox[3] - component.bbox[1] >= 3
        })
        .map(|(index, _)| index)
        .collect();
    if ordered.len() < 2 {
        return Some(None);
    }
    ordered.sort_by_key(|index| {
        let component = components[*index];
        (
            component.bbox[1],
            component.bbox[3],
            component.bbox[0],
            component.bbox[2],
            component_ids[*index],
        )
    });
    let count = ordered.len();
    let total_pixels: usize = ordered.iter().map(|index| components[*index].pixels).sum();
    let content_left = ordered
        .iter()
        .map(|index| components[*index].bbox[0])
        .min()?;
    let content_right = ordered
        .iter()
        .map(|index| components[*index].bbox[2])
        .max()?;
    let mut prefix_pixels = Vec::with_capacity(count);
    let mut prefix_bottoms = Vec::with_capacity(count);
    let mut prefix_lefts = Vec::with_capacity(count);
    let mut prefix_rights = Vec::with_capacity(count);
    let mut pixels = 0_usize;
    let mut bottom = bbox[1];
    let mut left = bbox[2];
    let mut right = bbox[0];
    for index in &ordered {
        let component = components[*index];
        pixels += component.pixels;
        bottom = bottom.max(component.bbox[3]);
        left = left.min(component.bbox[0]);
        right = right.max(component.bbox[2]);
        prefix_pixels.push(pixels);
        prefix_bottoms.push(bottom);
        prefix_lefts.push(left);
        prefix_rights.push(right);
    }
    let mut suffix_lefts = vec![bbox[2]; count];
    let mut suffix_rights = vec![bbox[0]; count];
    left = bbox[2];
    right = bbox[0];
    for ordered_index in (0..count).rev() {
        let component = components[ordered[ordered_index]];
        left = left.min(component.bbox[0]);
        right = right.max(component.bbox[2]);
        suffix_lefts[ordered_index] = left;
        suffix_rights[ordered_index] = right;
    }
    let required_width =
        1_usize.max(((content_right - content_left) as f64 * 0.15).round_ties_even() as usize);
    let mut candidates = Vec::<(f64, isize, usize, usize, usize)>::new();
    for index in 1..count {
        let coordinate = prefix_bottoms[index - 1];
        if !(bbox[1] < coordinate
            && coordinate < bbox[3]
            && coordinate <= components[ordered[index]].bbox[1])
        {
            continue;
        }
        let upper_pixels = prefix_pixels[index - 1];
        let lower_pixels = total_pixels - upper_pixels;
        let pixel_balance =
            upper_pixels.min(lower_pixels) as f64 / upper_pixels.max(lower_pixels) as f64;
        if pixel_balance < 0.12 {
            continue;
        }
        let upper_width = prefix_rights[index - 1] - prefix_lefts[index - 1];
        let lower_width = suffix_rights[index] - suffix_lefts[index];
        if upper_width < required_width || lower_width < required_width {
            continue;
        }
        let component_balance = index.min(count - index) as f64 / index.max(count - index) as f64;
        candidates.push((
            pixel_balance + 0.25 * component_balance,
            -(coordinate as isize),
            index,
            upper_pixels,
            lower_pixels,
        ));
    }
    candidates.sort_by(|first, second| {
        second
            .0
            .total_cmp(&first.0)
            .then_with(|| second.1.cmp(&first.1))
    });
    for (_, negative_coordinate, index, upper_pixels, lower_pixels) in candidates {
        let coordinate = (-negative_coordinate) as usize;
        let upper_indexes: Vec<usize> = components
            .iter()
            .enumerate()
            .filter(|(_, component)| component.bbox[3] <= coordinate)
            .map(|(component_index, _)| component_index)
            .collect();
        let lower_indexes: Vec<usize> = components
            .iter()
            .enumerate()
            .filter(|(_, component)| component.bbox[1] >= coordinate)
            .map(|(component_index, _)| component_index)
            .collect();
        if upper_indexes.is_empty()
            || lower_indexes.is_empty()
            || upper_indexes.len() + lower_indexes.len() != components.len()
        {
            continue;
        }
        if upper_pixels < lower_pixels {
            let guard_top = bbox[1].max(coordinate - 1);
            let guard_bottom = bbox[3].min(coordinate + 1);
            let local_width = bbox[2] - bbox[0];
            let mut bridge_columns = vec![false; local_width];
            for row in guard_top..guard_bottom {
                for (column, value) in bridge_columns.iter_mut().enumerate() {
                    *value |= diacritic_guard[row * width + bbox[0] + column] != 0;
                }
            }
            let lookback = 3_usize.max(2 * (guard_bottom - guard_top));
            let nearest_upper_bottom = prefix_bottoms[index - 1];
            let window_top = bbox[1].max(nearest_upper_bottom.saturating_sub(lookback));
            let window_height = coordinate - window_top;
            let mut upper_window = vec![0_u8; window_height * local_width];
            for component_index in &upper_indexes {
                for run in &component_runs[*component_index] {
                    if run.row < window_top || run.row >= coordinate {
                        continue;
                    }
                    let local_left = bbox[0].max(run.start) - bbox[0];
                    let local_right = bbox[2].min(run.stop) - bbox[0];
                    if local_right > local_left {
                        upper_window[(run.row - window_top) * local_width + local_left
                            ..(run.row - window_top) * local_width + local_right]
                            .fill(1);
                    }
                }
            }
            let mut upper_columns = vec![false; local_width];
            let mut upper_ink_columns = vec![false; local_width];
            for row in 0..window_height {
                for column in 0..local_width {
                    if upper_window[row * local_width + column] == 0 {
                        continue;
                    }
                    upper_ink_columns[column] = true;
                    let row_start = row.saturating_sub(1);
                    let row_stop = (row + 1).min(window_height.saturating_sub(1));
                    let column_start = column.saturating_sub(1);
                    let column_stop = (column + 1).min(local_width - 1);
                    'neighbor: for neighbor_row in row_start..=row_stop {
                        for neighbor_column in column_start..=column_stop {
                            if neighbor_row == row && neighbor_column == column {
                                continue;
                            }
                            if upper_window[neighbor_row * local_width + neighbor_column] != 0 {
                                upper_columns[column] = true;
                                break 'neighbor;
                            }
                        }
                    }
                }
            }
            for column in 0..local_width {
                upper_columns[column] |= upper_ink_columns[column] && bridge_columns[column];
            }
            let guarded = upper_columns
                .iter()
                .zip(&bridge_columns)
                .filter(|(upper, bridge)| **upper && **bridge)
                .count();
            let upper_count = upper_columns.iter().filter(|value| **value).count();
            if guarded as f64 / upper_count.max(1) as f64 >= 0.6 {
                continue;
            }
        }
        return Some(Some(ComponentRowBoundary {
            coordinate,
            upper_indexes,
            lower_indexes,
        }));
    }
    Some(None)
}

pub fn orientation_candidates(
    mask: &[u8],
    width: usize,
    height: usize,
    horizontal: bool,
    minimum_length: usize,
) -> Option<Vec<u8>> {
    if width == 0 || height == 0 || mask.len() != width.checked_mul(height)? {
        return None;
    }
    let mut output = vec![0; mask.len()];
    let (line_count, line_length) = if horizontal {
        (height, width)
    } else {
        (width, height)
    };
    for line in 0..line_count {
        let mut coordinate = 0;
        while coordinate < line_length {
            let index = if horizontal {
                line * width + coordinate
            } else {
                coordinate * width + line
            };
            if mask[index] == 0 {
                coordinate += 1;
                continue;
            }
            let start = coordinate;
            while coordinate < line_length {
                let index = if horizontal {
                    line * width + coordinate
                } else {
                    coordinate * width + line
                };
                if mask[index] == 0 {
                    break;
                }
                coordinate += 1;
            }
            if coordinate - start >= minimum_length {
                for selected in start..coordinate {
                    let index = if horizontal {
                        line * width + selected
                    } else {
                        selected * width + line
                    };
                    output[index] = 1;
                }
            }
        }
    }
    Some(output)
}

pub fn fill_short_gaps(
    mask: &[u8],
    width: usize,
    height: usize,
    horizontal: bool,
    maximum_gap: usize,
) -> Option<Vec<u8>> {
    if width == 0 || height == 0 || mask.len() != width.checked_mul(height)? {
        return None;
    }
    let mut output = mask.to_vec();
    let (line_count, line_length) = if horizontal {
        (height, width)
    } else {
        (width, height)
    };
    for line in 0..line_count {
        let mut coordinate = 0;
        while coordinate < line_length {
            let index = if horizontal {
                line * width + coordinate
            } else {
                coordinate * width + line
            };
            if output[index] != 0 {
                coordinate += 1;
                continue;
            }
            let start = coordinate;
            while coordinate < line_length {
                let index = if horizontal {
                    line * width + coordinate
                } else {
                    coordinate * width + line
                };
                if output[index] != 0 {
                    break;
                }
                coordinate += 1;
            }
            if start > 0 && coordinate < line_length && coordinate - start <= maximum_gap {
                for selected in start..coordinate {
                    let index = if horizontal {
                        line * width + selected
                    } else {
                        selected * width + line
                    };
                    output[index] = 1;
                }
            }
        }
    }
    Some(output)
}

fn has_lateral_clearance(
    mask: &[u8],
    width: usize,
    height: usize,
    bbox: [usize; 4],
    horizontal: bool,
) -> bool {
    let [left, top, right, bottom] = bbox;
    let band = 4;
    let density = |x0: usize, y0: usize, x1: usize, y1: usize| -> Option<f64> {
        if x0 >= x1 || y0 >= y1 {
            return None;
        }
        let pixels = (y0..y1)
            .flat_map(|y| (x0..x1).map(move |x| (x, y)))
            .filter(|(x, y)| mask[y * width + x] != 0)
            .count();
        Some(pixels as f64 / ((x1 - x0) * (y1 - y0)) as f64)
    };
    let (first, second) = if horizontal {
        (
            density(left, top.saturating_sub(band), right, top),
            density(left, bottom, right, (bottom + band).min(height)),
        )
    } else {
        (
            density(left.saturating_sub(band), top, left, bottom),
            density(right, top, (right + band).min(width), bottom),
        )
    };
    matches!((first, second), (Some(first), Some(second)) if first.max(second) <= 0.25)
}

fn percentile_99(mut values: Vec<u8>) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    values.sort_unstable();
    let index = (values.len() - 1) as f64 * 0.99;
    let lower = index.floor() as usize;
    let upper = index.ceil() as usize;
    let fraction = index - lower as f64;
    f64::from(values[lower]) * (1.0 - fraction) + f64::from(values[upper]) * fraction
}

pub fn color_rule_drafts(rgb: &[u8], width: usize, height: usize) -> Option<Vec<RuleDraftSummary>> {
    if width == 0 || height == 0 || rgb.len() != width.checked_mul(height)?.checked_mul(3)? {
        return None;
    }
    let sample_step = 1_usize.max(width.max(height).div_ceil(512));
    let mut chroma = Vec::new();
    for y in (0..height).step_by(sample_step) {
        for x in (0..width).step_by(sample_step) {
            let offset = (y * width + x) * 3;
            let color = &rgb[offset..offset + 3];
            chroma.push(color.iter().max()?.saturating_sub(*color.iter().min()?));
        }
    }
    if percentile_99(chroma) < 12.0 {
        return Some(Vec::new());
    }
    let max_rule_thickness = 4;
    let min_rule_length = 24;
    let min_rule_page_fraction = 0.6_f64;
    let min_rule_aspect_ratio = 12.0_f64;
    let mut horizontal_evidence = vec![0_u8; width * height];
    let maximum_offset = (max_rule_thickness + 1).min(1_usize.max((height - 1) / 2));
    for offset in 1..=maximum_offset {
        if height <= 2 * offset {
            break;
        }
        for y in offset..height - offset {
            for x in 0..width {
                let center = (y * width + x) * 3;
                let above = ((y - offset) * width + x) * 3;
                let below = ((y + offset) * width + x) * 3;
                let differs_above = (0..3)
                    .map(|channel| rgb[center + channel].abs_diff(rgb[above + channel]))
                    .max()
                    .unwrap_or(0)
                    >= 12;
                let differs_below = (0..3)
                    .map(|channel| rgb[center + channel].abs_diff(rgb[below + channel]))
                    .max()
                    .unwrap_or(0)
                    >= 12;
                let stable_flanks = (0..3)
                    .map(|channel| rgb[above + channel].abs_diff(rgb[below + channel]))
                    .max()
                    .unwrap_or(0)
                    <= 8;
                horizontal_evidence[y * width + x] |=
                    u8::from(differs_above && differs_below && stable_flanks);
            }
        }
    }
    horizontal_evidence = fill_short_gaps(
        &horizontal_evidence,
        width,
        height,
        true,
        max_rule_thickness,
    )?;
    let mut vertical_evidence = vec![0_u8; width * height];
    for y in 0..height {
        for x in 1..width {
            let current = (y * width + x) * 3;
            let previous = current - 3;
            vertical_evidence[y * width + x] = u8::from(
                (0..3)
                    .map(|channel| rgb[current + channel].abs_diff(rgb[previous + channel]))
                    .max()
                    .unwrap_or(0)
                    >= 12,
            );
        }
    }
    vertical_evidence =
        fill_short_gaps(&vertical_evidence, width, height, false, max_rule_thickness)?;
    let minimum_horizontal = width.min(min_rule_length);
    let mut horizontal = Vec::new();
    if minimum_horizontal >= 2 {
        let candidates = orientation_candidates(
            &horizontal_evidence,
            width,
            height,
            true,
            minimum_horizontal,
        )?;
        for component in connected_components(&candidates, width, height)? {
            let component_width = component.bbox[2] - component.bbox[0];
            let component_height = component.bbox[3] - component.bbox[1];
            let minimum_width =
                (width as f64 * 0.8_f64.max(min_rule_page_fraction)).round_ties_even() as usize;
            if component_height <= max_rule_thickness.max(1)
                && component_width >= minimum_width
                && component_width as f64 / component_height as f64 >= min_rule_aspect_ratio
            {
                horizontal.push(RuleDraftSummary {
                    horizontal: true,
                    bbox: component.bbox,
                });
            }
        }
    }
    if horizontal.len() < 3 {
        return Some(Vec::new());
    }
    let minimum_vertical = height.min(min_rule_length);
    let mut vertical = Vec::new();
    if minimum_vertical >= 2 {
        let candidates =
            orientation_candidates(&vertical_evidence, width, height, false, minimum_vertical)?;
        for component in connected_components(&candidates, width, height)? {
            let crossings = horizontal
                .iter()
                .filter(|rule| boxes_intersect(rule.bbox, component.bbox))
                .count();
            let component_width = component.bbox[2] - component.bbox[0];
            let component_height = component.bbox[3] - component.bbox[1];
            if crossings >= 2
                && component_width <= max_rule_thickness.max(1)
                && component_height as f64 / component_width as f64 >= min_rule_aspect_ratio
            {
                vertical.push(RuleDraftSummary {
                    horizontal: false,
                    bbox: component.bbox,
                });
            }
        }
    }
    horizontal.extend(vertical);
    Some(horizontal)
}

fn boxes_intersect(first: [usize; 4], second: [usize; 4]) -> bool {
    first[0].max(second[0]) < first[2].min(second[2])
        && first[1].max(second[1]) < first[3].min(second[3])
}

#[derive(Clone)]
struct MaskRuleDraft {
    summary: RuleDraftSummary,
    candidate_mask: Arc<[u8]>,
    claim_full_bbox: bool,
}

fn page_edge_band(
    candidate: &[u8],
    width: usize,
    height: usize,
    bbox: [usize; 4],
    horizontal: bool,
) -> Option<[usize; 4]> {
    let [left, top, right, bottom] = bbox;
    let perpendicular_size = if horizontal { height } else { width };
    let axial_span = if horizontal {
        right - left
    } else {
        bottom - top
    };
    let mut dense = vec![false; perpendicular_size];
    if horizontal {
        for y in 0..height {
            let projection = (left..right)
                .filter(|x| candidate[y * width + x] != 0)
                .count();
            dense[y] = projection >= (axial_span as f64 * 0.8).ceil() as usize;
        }
    } else {
        for x in 0..width {
            let projection = (top..bottom)
                .filter(|y| candidate[y * width + x] != 0)
                .count();
            dense[x] = projection >= (axial_span as f64 * 0.8).ceil() as usize;
        }
    }
    let perpendicular_start = if horizontal { top } else { left };
    let perpendicular_stop = if horizontal { bottom } else { right };
    if perpendicular_start == 0 {
        let mut stop = 0;
        while stop < perpendicular_size && dense[stop] {
            stop += 1;
        }
        if stop == 0 {
            return None;
        }
        return Some(if horizontal {
            [left, 0, right, stop]
        } else {
            [0, top, stop, bottom]
        });
    }
    if perpendicular_stop == perpendicular_size {
        let mut start = perpendicular_size;
        while start > 0 && dense[start - 1] {
            start -= 1;
        }
        if start == perpendicular_size {
            return None;
        }
        return Some(if horizontal {
            [left, start, right, perpendicular_size]
        } else {
            [start, top, perpendicular_size, bottom]
        });
    }
    None
}

fn collect_mask_rule_drafts(
    source: &[u8],
    width: usize,
    height: usize,
    frame_edges: bool,
) -> Option<Vec<MaskRuleDraft>> {
    let horizontal_minimum = width.min(24);
    let vertical_minimum = height.min(24);
    let effective_thickness =
        4_usize.max((width.min(height) as f64 * 0.003).round_ties_even() as usize);
    let mut values = Vec::new();
    for (horizontal, minimum) in [(true, horizontal_minimum), (false, vertical_minimum)] {
        if minimum < 2 {
            continue;
        }
        let candidate = orientation_candidates(source, width, height, horizontal, minimum)?;
        let candidate = Arc::<[u8]>::from(candidate);
        let source_mask = Arc::<[u8]>::from(source.to_vec());
        for component in connected_components(&candidate, width, height)? {
            let thickness = if horizontal {
                component.bbox[3] - component.bbox[1]
            } else {
                component.bbox[2] - component.bbox[0]
            };
            let length = if horizontal {
                component.bbox[2] - component.bbox[0]
            } else {
                component.bbox[3] - component.bbox[1]
            };
            let aspect_ratio = length as f64 / thickness.max(1) as f64;
            let frame_thickness_limit = 16_usize.max(4 * effective_thickness);
            let frame_bbox = page_edge_band(&candidate, width, height, component.bbox, horizontal);
            let frame_thickness = frame_bbox.map_or(0, |bbox| {
                if horizontal {
                    bbox[3] - bbox[1]
                } else {
                    bbox[2] - bbox[0]
                }
            });
            let axis_size = if horizontal { width } else { height };
            let is_frame_edge = length
                >= 24_usize.max((axis_size as f64 * 0.20).round_ties_even() as usize)
                && frame_bbox.is_some()
                && frame_thickness <= frame_thickness_limit;
            let is_structural_rule = thickness <= effective_thickness
                && length >= minimum
                && aspect_ratio >= 12.0
                && has_lateral_clearance(source, width, height, component.bbox, horizontal);
            if frame_edges && is_frame_edge {
                let complete_component_is_bounded = thickness <= frame_thickness_limit;
                let partial_axis = length < (axis_size as f64 * 0.80).round_ties_even() as usize;
                values.push(MaskRuleDraft {
                    summary: RuleDraftSummary {
                        horizontal,
                        bbox: if complete_component_is_bounded && partial_axis {
                            component.bbox
                        } else {
                            frame_bbox?
                        },
                    },
                    candidate_mask: Arc::clone(&source_mask),
                    claim_full_bbox: complete_component_is_bounded && partial_axis,
                });
            } else if !frame_edges && is_structural_rule {
                values.push(MaskRuleDraft {
                    summary: RuleDraftSummary {
                        horizontal,
                        bbox: component.bbox,
                    },
                    candidate_mask: Arc::clone(&candidate),
                    claim_full_bbox: false,
                });
            }
        }
    }
    Some(values)
}

fn closed_page_frame(
    drafts: Vec<MaskRuleDraft>,
    width: usize,
    height: usize,
) -> Vec<MaskRuleDraft> {
    let has_top = drafts
        .iter()
        .any(|draft| draft.summary.horizontal && draft.summary.bbox[1] == 0);
    let has_bottom = drafts
        .iter()
        .any(|draft| draft.summary.horizontal && draft.summary.bbox[3] == height);
    let has_left = drafts
        .iter()
        .any(|draft| !draft.summary.horizontal && draft.summary.bbox[0] == 0);
    let has_right = drafts
        .iter()
        .any(|draft| !draft.summary.horizontal && draft.summary.bbox[2] == width);
    if ((has_top || has_bottom) && (has_left || has_right)) || (has_top && has_bottom) {
        drafts
    } else {
        Vec::new()
    }
}

fn bbox_union<'a>(boxes: impl IntoIterator<Item = &'a [usize; 4]>) -> [usize; 4] {
    let mut boxes = boxes.into_iter();
    let first = *boxes.next().expect("non-empty rule network");
    boxes.fold(first, |union, bbox| {
        [
            union[0].min(bbox[0]),
            union[1].min(bbox[1]),
            union[2].max(bbox[2]),
            union[3].max(bbox[3]),
        ]
    })
}

fn detect_mask_rule_drafts_internal(
    mask: &[u8],
    width: usize,
    height: usize,
) -> Option<Vec<MaskRuleDraft>> {
    if width == 0 || height == 0 || mask.len() != width.checked_mul(height)? {
        return None;
    }
    let frame_drafts = closed_page_frame(
        collect_mask_rule_drafts(mask, width, height, true)?,
        width,
        height,
    );
    let mut frame_mask = vec![0_u8; mask.len()];
    for draft in &frame_drafts {
        let [left, top, right, bottom] = draft.summary.bbox;
        for y in top..bottom {
            for x in left..right {
                frame_mask[y * width + x] |= draft.candidate_mask[y * width + x];
            }
        }
    }
    let interior: Vec<u8> = mask
        .iter()
        .zip(frame_mask)
        .map(|(value, frame)| u8::from(*value != 0 && frame == 0))
        .collect();
    let candidates = collect_mask_rule_drafts(&interior, width, height, false)?;
    let mut accepted = std::collections::BTreeSet::<usize>::new();
    for (index, candidate) in candidates.iter().enumerate() {
        let axis_length = if candidate.summary.horizontal {
            width
        } else {
            height
        };
        let length = if candidate.summary.horizontal {
            candidate.summary.bbox[2] - candidate.summary.bbox[0]
        } else {
            candidate.summary.bbox[3] - candidate.summary.bbox[1]
        };
        let minimum =
            (2 * 24).max((axis_length as f64 * 0.8_f64.max(0.6)).round_ties_even() as usize);
        if length >= minimum {
            accepted.insert(index);
        }
    }
    let mut unseen: std::collections::BTreeSet<usize> = (0..candidates.len()).collect();
    while let Some(seed) = unseen.pop_first() {
        let mut network = std::collections::BTreeSet::from([seed]);
        let mut frontier = vec![seed];
        while let Some(current) = frontier.pop() {
            let current_candidate = &candidates[current];
            let neighbors: Vec<usize> = unseen
                .iter()
                .copied()
                .filter(|index| {
                    candidates[*index].summary.horizontal != current_candidate.summary.horizontal
                        && boxes_intersect(
                            candidates[*index].summary.bbox,
                            current_candidate.summary.bbox,
                        )
                })
                .collect();
            for neighbor in neighbors {
                unseen.remove(&neighbor);
                network.insert(neighbor);
                frontier.push(neighbor);
            }
        }
        let horizontal_count = network
            .iter()
            .filter(|index| candidates[**index].summary.horizontal)
            .count();
        let vertical_count = network.len() - horizontal_count;
        let network_bbox = bbox_union(network.iter().map(|index| &candidates[*index].summary.bbox));
        if horizontal_count >= 2
            && vertical_count >= 2
            && network_bbox[2] - network_bbox[0] >= 48
            && network_bbox[3] - network_bbox[1] >= 48
        {
            accepted.extend(network);
        }
    }
    loop {
        let promoted: Vec<usize> = candidates
            .iter()
            .enumerate()
            .filter(|(index, candidate)| {
                !accepted.contains(index)
                    && accepted.iter().any(|accepted| {
                        candidate.summary.horizontal != candidates[*accepted].summary.horizontal
                            && boxes_intersect(
                                candidate.summary.bbox,
                                candidates[*accepted].summary.bbox,
                            )
                    })
            })
            .map(|(index, _)| index)
            .collect();
        if promoted.is_empty() {
            break;
        }
        accepted.extend(promoted);
    }
    let mut drafts = frame_drafts;
    drafts.extend(accepted.into_iter().map(|index| candidates[index].clone()));
    drafts.sort_by_key(|draft| {
        (
            !draft.summary.horizontal,
            draft.summary.bbox[1],
            draft.summary.bbox[0],
            draft.summary.bbox[3],
            draft.summary.bbox[2],
        )
    });
    Some(drafts)
}

pub fn mask_rule_drafts(mask: &[u8], width: usize, height: usize) -> Option<Vec<RuleDraftSummary>> {
    Some(
        detect_mask_rule_drafts_internal(mask, width, height)?
            .into_iter()
            .map(|draft| draft.summary)
            .collect(),
    )
}

fn find_component_label(parents: &mut [usize], mut label: usize) -> usize {
    while parents[label] != label {
        parents[label] = parents[parents[label]];
        label = parents[label];
    }
    label
}

fn union_component_labels(parents: &mut [usize], first: usize, second: usize) {
    let first_root = find_component_label(parents, first);
    let second_root = find_component_label(parents, second);
    if first_root != second_root {
        let lower = first_root.min(second_root);
        let higher = first_root.max(second_root);
        parents[higher] = lower;
    }
}

fn grayscale(color: [u8; 3]) -> i32 {
    (299 * i32::from(color[0]) + 587 * i32::from(color[1]) + 114 * i32::from(color[2]) + 500)
        / 1_000
}

fn pillow_gaussian_box_radius(radius: f64) -> f64 {
    let passes = 3.0_f64;
    let sigma_squared = radius * radius / passes;
    let length = (12.0 * sigma_squared + 1.0).sqrt();
    let integer = ((length - 1.0) / 2.0).floor();
    let numerator = (2.0 * integer + 1.0) * (integer * (integer + 1.0) - 3.0 * sigma_squared);
    let denominator = 6.0 * (sigma_squared - (integer + 1.0) * (integer + 1.0));
    integer + numerator / denominator
}

fn pillow_blur_line(source: &[u8], float_radius: f64) -> Vec<u8> {
    if source.is_empty() || float_radius == 0.0 {
        return source.to_vec();
    }
    let radius = float_radius as usize;
    let scale = 1_u64 << 24;
    let whole_weight = (scale as f64 / (float_radius * 2.0 + 1.0)) as u64;
    let far_weight = (scale - (radius * 2 + 1) as u64 * whole_weight) / 2;
    let last = source.len() - 1;
    let sample =
        |index: isize| -> u64 { u64::from(source[index.clamp(0, last as isize) as usize]) };
    let mut accumulator = (-(radius as isize)..=radius as isize)
        .map(sample)
        .sum::<u64>();
    let mut output = Vec::with_capacity(source.len());
    for x in 0..source.len() {
        let center = x as isize;
        let bulk = accumulator * whole_weight
            + (sample(center - radius as isize - 1) + sample(center + radius as isize + 1))
                * far_weight;
        output.push(((bulk + (1 << 23)) >> 24) as u8);
        if x + 1 < source.len() {
            accumulator = accumulator
                .saturating_sub(sample(center - radius as isize))
                .saturating_add(sample(center + radius as isize + 1));
        }
    }
    output
}

fn pillow_box_blur_horizontal(source: &[u8], width: usize, height: usize, radius: f64) -> Vec<u8> {
    let mut output = vec![0_u8; source.len()];
    for y in 0..height {
        let blurred = pillow_blur_line(&source[y * width..(y + 1) * width], radius);
        output[y * width..(y + 1) * width].copy_from_slice(&blurred);
    }
    output
}

fn pillow_box_blur_vertical(source: &[u8], width: usize, height: usize, radius: f64) -> Vec<u8> {
    let mut output = vec![0_u8; source.len()];
    let mut column = vec![0_u8; height];
    for x in 0..width {
        for y in 0..height {
            column[y] = source[y * width + x];
        }
        let blurred = pillow_blur_line(&column, radius);
        for y in 0..height {
            output[y * width + x] = blurred[y];
        }
    }
    output
}

fn gaussian_blur(gray: &[i32], width: usize, height: usize, sigma: usize) -> Vec<i32> {
    let radius = pillow_gaussian_box_radius(sigma as f64);
    let mut values: Vec<u8> = gray.iter().map(|value| *value as u8).collect();
    for _ in 0..3 {
        values = pillow_box_blur_horizontal(&values, width, height, radius);
    }
    for _ in 0..3 {
        values = pillow_box_blur_vertical(&values, width, height, radius);
    }
    values.into_iter().map(i32::from).collect()
}

fn clean_axis_bands(mask: &mut [bool], width: usize, height: usize) {
    let active_rows: Vec<usize> = (0..height)
        .filter(|y| {
            mask[*y * width..(*y + 1) * width]
                .iter()
                .any(|value| *value)
        })
        .collect();
    let active_columns: Vec<usize> = (0..width)
        .filter(|x| (0..height).any(|y| mask[y * width + *x]))
        .collect();
    let (Some(top), Some(bottom), Some(left), Some(right)) = (
        active_rows.first().copied(),
        active_rows.last().copied(),
        active_columns.first().copied(),
        active_columns.last().copied(),
    ) else {
        return;
    };
    let active_height = bottom - top + 1;
    let active_width = right - left + 1;
    let covered_columns: Vec<bool> = (0..width)
        .map(|x| {
            (top..=bottom).filter(|y| mask[*y * width + x]).count() * 100 >= active_height * 88
        })
        .collect();
    let covered_rows: Vec<bool> = (0..height)
        .map(|y| (left..=right).filter(|x| mask[y * width + *x]).count() * 100 >= active_width * 88)
        .collect();
    for y in 0..height {
        for x in 0..width {
            if covered_columns[x] || covered_rows[y] {
                mask[y * width + x] = false;
            }
        }
    }
}

fn select_layout_foreground_with_force(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
    physical: &[bool],
    force_adaptive: bool,
) -> (Vec<bool>, String) {
    if !force_adaptive {
        let edge_prefix = |horizontal: bool, reverse: bool| {
            let length = if horizontal { height } else { width };
            let other = if horizontal { width } else { height };
            let mut count = 0;
            for offset in 0..length {
                let index = if reverse { length - 1 - offset } else { offset };
                let ink = (0..other)
                    .filter(|cross| {
                        let (x, y) = if horizontal {
                            (*cross, index)
                        } else {
                            (index, *cross)
                        };
                        physical[y * width + x]
                    })
                    .count();
                if ink * 100 < other * 80 {
                    break;
                }
                count += 1;
            }
            count
        };
        let top = edge_prefix(true, false);
        let bottom = edge_prefix(true, true);
        let left = edge_prefix(false, false);
        let right = edge_prefix(false, true);
        if [top, bottom, left, right].iter().all(|value| *value > 0)
            && top + bottom < height
            && left + right < width
        {
            let interior_pixels = (height - top - bottom) * (width - left - right);
            let interior_ink = (top..height - bottom)
                .flat_map(|y| (left..width - right).map(move |x| (x, y)))
                .filter(|(x, y)| physical[*y * width + *x])
                .count();
            if interior_pixels == 0 || interior_ink * 100 <= interior_pixels * 30 {
                return (physical.to_vec(), "physical-frame-interior".to_owned());
            }
        }
    }

    let colors: Vec<[u8; 3]> = (0..height)
        .flat_map(|y| (0..width).map(move |x| (x, y)))
        .map(|(x, y)| rgb_at(pixels, stride, channels, x, y))
        .collect();
    let gray: Vec<i32> = colors.iter().copied().map(grayscale).collect();
    let radius = (width.min(height) / 220).clamp(3, 12);
    let blurred = gaussian_blur(&gray, width, height, radius);
    let overlay: Vec<bool> = colors
        .iter()
        .zip(&gray)
        .map(|(color, gray)| {
            let high = *color.iter().max().unwrap_or(&0);
            let low = *color.iter().min().unwrap_or(&0);
            high.saturating_sub(low) >= 38 && high >= 145 && *gray >= 115
        })
        .collect();
    let mut selected = vec![false; width * height];
    let mut selected_deltas = Vec::with_capacity(2);
    let mut modes = Vec::with_capacity(2);
    for (name, light) in [("local_dark", false), ("local_light", true)] {
        let mut best: Option<(f64, i32, Vec<bool>)> = None;
        for delta in [10_i32, 14, 18, 22, 28] {
            let mask: Vec<bool> = gray
                .iter()
                .zip(&blurred)
                .zip(&overlay)
                .map(|((value, local), excluded)| {
                    !*excluded
                        && if light {
                            *value - delta > *local
                        } else {
                            *value + delta < *local
                        }
                })
                .collect();
            let ratio = mask.iter().filter(|value| **value).count() as f64 / mask.len() as f64;
            let valid = (0.002..=0.28).contains(&ratio);
            let score = if valid { 0.0 } else { 1.0 } + (ratio - 0.065).abs();
            if best.as_ref().is_none_or(|current| score < current.0) {
                best = Some((score, delta, mask));
            }
        }
        let (_score, delta, mut mask) = best.expect("five adaptive candidates");
        clean_axis_bands(&mut mask, width, height);
        for (target, value) in selected.iter_mut().zip(mask) {
            *target |= value;
        }
        selected_deltas.push(delta);
        modes.push(format!("{name}:{delta}"));
    }
    if !selected.iter().any(|value| *value) {
        if force_adaptive {
            return (vec![false; physical.len()], "adaptive-empty".to_owned());
        }
        return (physical.to_vec(), "physical-fallback".to_owned());
    }
    if force_adaptive {
        return (selected, format!("adaptive-dual:{}", modes.join("+")));
    }
    let total = width * height;
    let physical_count = physical.iter().filter(|value| **value).count();
    let selected_count = selected.iter().filter(|value| **value).count();
    let suppressed_count = physical
        .iter()
        .zip(&selected)
        .filter(|(source, adaptive)| **source && !**adaptive)
        .count();
    let anchored_count = selected
        .iter()
        .zip(physical)
        .filter(|(adaptive, source)| **adaptive && **source)
        .count();
    let selected_delta = *selected_deltas.iter().min().unwrap_or(&10);
    let mut flat_suppressed = 0_usize;
    for y in 0..height {
        for x in 0..width {
            let index = y * width + x;
            if !physical[index] || selected[index] {
                continue;
            }
            let color = colors[index];
            let mut variation = 0_u8;
            for (nx, ny) in [
                (x.saturating_sub(1), y),
                ((x + 1).min(width - 1), y),
                (x, y.saturating_sub(1)),
                (x, (y + 1).min(height - 1)),
            ] {
                let neighbor = colors[ny * width + nx];
                variation = variation.max(
                    color
                        .iter()
                        .zip(neighbor)
                        .map(|(left, right)| left.abs_diff(right))
                        .max()
                        .unwrap_or(0),
                );
            }
            flat_suppressed += usize::from(i32::from(variation) <= selected_delta);
        }
    }
    let fill_dominates = physical_count.saturating_sub(selected_count) * 100 >= total * 15
        && physical_count * 100 >= selected_count * 400
        && anchored_count * 100 >= selected_count.max(1) * 80
        && flat_suppressed * 3 >= suppressed_count.max(1) * 2;
    let mut quantized = vec![0_u32; 32_768];
    for color in &colors {
        quantized[quantized_key(*color)] += 1;
    }
    let dominant = usize::try_from(quantized.into_iter().max().unwrap_or(0)).unwrap_or(0);
    let unstable_paper = physical_count * 100 >= total * 30
        && physical_count.saturating_sub(selected_count) * 100 >= total * 15
        && selected_count * 100 <= total * 20
        && dominant * 100 < total * 15;
    if !fill_dominates && !unstable_paper {
        return (physical.to_vec(), "physical".to_owned());
    }
    let suffix = if unstable_paper && !fill_dominates {
        ":unstable-paper-background"
    } else {
        ""
    };
    (
        selected,
        format!("adaptive-dual:{}{suffix}", modes.join("+")),
    )
}

fn select_layout_foreground(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
    physical: &[bool],
) -> (Vec<bool>, String) {
    select_layout_foreground_with_force(pixels, width, height, stride, channels, physical, false)
}

pub fn select_layout_foreground_mask(
    rgb: &[u8],
    width: usize,
    height: usize,
    physical: &[u8],
    force_adaptive: bool,
) -> Option<(Vec<u8>, String)> {
    if width == 0
        || height == 0
        || rgb.len() != width.checked_mul(height)?.checked_mul(3)?
        || physical.len() != width.checked_mul(height)?
    {
        return None;
    }
    let physical: Vec<bool> = physical.iter().map(|value| *value != 0).collect();
    let (selected, mode) = select_layout_foreground_with_force(
        rgb,
        width,
        height,
        width * 3,
        3,
        &physical,
        force_adaptive,
    );
    Some((selected.into_iter().map(u8::from).collect(), mode))
}

fn max_pool_mask(
    mask: &[bool],
    width: usize,
    height: usize,
    maximum: usize,
) -> (Vec<bool>, usize, usize) {
    let factor = width.max(height).div_ceil(maximum).max(1);
    if factor == 1 {
        return (mask.to_vec(), width, height);
    }
    let pooled_width = width.div_ceil(factor);
    let pooled_height = height.div_ceil(factor);
    let mut pooled = vec![false; pooled_width * pooled_height];
    for y in 0..height {
        for x in 0..width {
            pooled[(y / factor) * pooled_width + x / factor] |= mask[y * width + x];
        }
    }
    (pooled, pooled_width, pooled_height)
}

fn projection_score(mask: &[bool], width: usize, height: usize, angle: f64) -> f64 {
    let radians = angle.to_radians();
    let cosine = radians.cos();
    let sine = radians.sin();
    let center_x = width as f64 / 2.0;
    let center_y = height as f64 / 2.0;
    let corners = [
        (0.0, 0.0),
        (width as f64, 0.0),
        (0.0, height as f64),
        (width as f64, height as f64),
    ];
    let minimum_y = corners
        .iter()
        .map(|(x, y)| -sine * (*x - center_x) + cosine * (*y - center_y))
        .fold(f64::INFINITY, f64::min)
        .floor();
    let maximum_y = corners
        .iter()
        .map(|(x, y)| -sine * (*x - center_x) + cosine * (*y - center_y))
        .fold(f64::NEG_INFINITY, f64::max)
        .ceil();
    let mut rows = vec![0_u32; (maximum_y - minimum_y).max(1.0) as usize];
    for y in 0..height {
        for x in 0..width {
            if !mask[y * width + x] {
                continue;
            }
            let mapped = -sine * (x as f64 + 0.5 - center_x) + cosine * (y as f64 + 0.5 - center_y)
                - minimum_y;
            let row = mapped
                .floor()
                .clamp(0.0, rows.len().saturating_sub(1) as f64) as usize;
            rows[row] += 1;
        }
    }
    let total: u64 = rows.iter().map(|value| u64::from(*value)).sum();
    if total == 0 {
        return 0.0;
    }
    rows.iter()
        .map(|value| f64::from(*value).powi(2))
        .sum::<f64>()
        / total as f64
}

fn estimate_correction(mask: &[bool], width: usize, height: usize) -> i32 {
    if mask.iter().filter(|value| **value).count() < 128 {
        return 0;
    }
    let (sample, sample_width, sample_height) = max_pool_mask(mask, width, height, 1_200);
    let baseline = projection_score(&sample, sample_width, sample_height, 0.0);
    let mut coarse_best = 0.0_f64;
    let mut coarse_score = f64::NEG_INFINITY;
    for step in -10..=10 {
        let angle = step as f64 * 0.5;
        let score = projection_score(&sample, sample_width, sample_height, angle);
        let key = (score, -angle.abs(), -angle);
        let best_key = (coarse_score, -coarse_best.abs(), -coarse_best);
        if key > best_key {
            coarse_score = score;
            coarse_best = angle;
        }
    }
    let start = (-5.0_f64).max(coarse_best - 0.5);
    let stop = 5.0_f64.min(coarse_best + 0.5);
    let mut best_angle = 0.0_f64;
    let mut best_score = f64::NEG_INFINITY;
    let steps = ((stop - start) / 0.1 + 1e-9).floor() as i32;
    for index in 0..=steps {
        let angle = (start + f64::from(index) * 0.1) * 10.0;
        let angle = angle.round() / 10.0;
        let score = projection_score(&sample, sample_width, sample_height, angle);
        let key = (score, -angle.abs(), -angle);
        let best_key = (best_score, -best_angle.abs(), -best_angle);
        if key > best_key {
            best_score = score;
            best_angle = angle;
        }
    }
    if best_score <= baseline * 1.02 || best_angle.abs() < 0.05 {
        0
    } else {
        (best_angle * 1_000.0).round() as i32
    }
}

impl ForegroundGeometry {
    pub fn foreground_pixels(&self) -> usize {
        self.foreground.iter().filter(|pixel| **pixel).count()
    }
}

fn pillow_floor(value: f64) -> i32 {
    value.floor() as i32
}

fn pillow_fixed(value: f64) -> i32 {
    pillow_floor(value * 65_536.0 + 0.5)
}

fn rgb_pixels(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> Vec<u8> {
    let mut output = Vec::with_capacity(width * height * 3);
    for y in 0..height {
        for x in 0..width {
            output.extend_from_slice(&rgb_at(pixels, stride, channels, x, y));
        }
    }
    output
}

fn bicubic_sample(
    pixels: &[u8],
    width: usize,
    height: usize,
    xin: f64,
    yin: f64,
    channel: usize,
) -> Option<u8> {
    if xin < 0.0 || xin >= width as f64 || yin < 0.0 || yin >= height as f64 {
        return None;
    }
    let xin = xin - 0.5;
    let yin = yin - 0.5;
    let x = pillow_floor(xin) - 1;
    let y = pillow_floor(yin) - 1;
    let dx = xin - f64::from(x + 1);
    let dy = yin - f64::from(y + 1);
    let cubic = |values: [f64; 4], distance: f64| {
        let [v1, v2, v3, v4] = values;
        let p1 = v2;
        let p2 = -v1 + v3;
        let p3 = 2.0 * (v1 - v2) + v3 - v4;
        let p4 = -v1 + v2 - v3 + v4;
        p1 + distance * (p2 + distance * (p3 + distance * p4))
    };
    let sample_row = |row: i32| {
        let row = row.clamp(0, height as i32 - 1) as usize;
        let mut values = [0.0; 4];
        for (slot, column) in values.iter_mut().zip(x..x + 4) {
            let column = column.clamp(0, width as i32 - 1) as usize;
            *slot = f64::from(pixels[(row * width + column) * 3 + channel]);
        }
        cubic(values, dx)
    };
    let first = sample_row(y);
    let second = if y + 1 >= 0 && y + 1 < height as i32 {
        sample_row(y + 1)
    } else {
        first
    };
    let third = if y + 2 >= 0 && y + 2 < height as i32 {
        sample_row(y + 2)
    } else {
        second
    };
    let fourth = if y + 3 >= 0 && y + 3 < height as i32 {
        sample_row(y + 3)
    } else {
        third
    };
    let value = cubic([first, second, third, fourth], dy);
    Some(if value <= 0.0 {
        0
    } else if value >= 255.0 {
        255
    } else {
        value as u8
    })
}

fn align_geometry(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
    foreground: Vec<bool>,
    background: [u8; 3],
    correction_milli_degrees: i32,
) -> (Vec<u8>, Vec<bool>, usize, usize) {
    let source_rgb = rgb_pixels(pixels, width, height, stride, channels);
    if correction_milli_degrees == 0 {
        return (source_rgb, foreground, width, height);
    }
    let radians = -(f64::from(correction_milli_degrees) / 1_000.0).to_radians();
    let cosine = radians.cos();
    let sine = radians.sin();
    let center_x = width as f64 / 2.0;
    let center_y = height as f64 / 2.0;
    let rotation = [
        cosine,
        -sine,
        center_x - cosine * center_x + sine * center_y,
        sine,
        cosine,
        center_y - sine * center_x - cosine * center_y,
    ];
    let transform = |x: f64, y: f64| {
        (
            rotation[0] * x + rotation[1] * y + rotation[2],
            rotation[3] * x + rotation[4] * y + rotation[5],
        )
    };
    let corners = [
        transform(0.0, 0.0),
        transform(width as f64, 0.0),
        transform(0.0, height as f64),
        transform(width as f64, height as f64),
    ];
    let minimum_x = corners
        .iter()
        .map(|point| point.0)
        .fold(f64::INFINITY, f64::min)
        .floor();
    let minimum_y = corners
        .iter()
        .map(|point| point.1)
        .fold(f64::INFINITY, f64::min)
        .floor();
    let maximum_x = corners
        .iter()
        .map(|point| point.0)
        .fold(f64::NEG_INFINITY, f64::max)
        .ceil();
    let maximum_y = corners
        .iter()
        .map(|point| point.1)
        .fold(f64::NEG_INFINITY, f64::max)
        .ceil();
    let output_width = (maximum_x - minimum_x) as usize;
    let output_height = (maximum_y - minimum_y) as usize;
    let forward = [
        rotation[0],
        rotation[1],
        rotation[2] - minimum_x,
        rotation[3],
        rotation[4],
        rotation[5] - minimum_y,
    ];
    let determinant = forward[0] * forward[4] - forward[1] * forward[3];
    let inverse = [
        forward[4] / determinant,
        -forward[1] / determinant,
        (forward[1] * forward[5] - forward[4] * forward[2]) / determinant,
        -forward[3] / determinant,
        forward[0] / determinant,
        (forward[3] * forward[2] - forward[0] * forward[5]) / determinant,
    ];

    let a0 = pillow_fixed(inverse[0]);
    let a1 = pillow_fixed(inverse[1]);
    let a3 = pillow_fixed(inverse[3]);
    let a4 = pillow_fixed(inverse[4]);
    let mut a2 = pillow_fixed(inverse[2] + inverse[0] * 0.5 + inverse[1] * 0.5);
    let mut a5 = pillow_fixed(inverse[5] + inverse[3] * 0.5 + inverse[4] * 0.5);
    let mut aligned_mask = vec![false; output_width * output_height];
    for y in 0..output_height {
        let mut xx = a2;
        let mut yy = a5;
        for x in 0..output_width {
            let source_x = xx >> 16;
            let source_y = yy >> 16;
            if source_x >= 0 && source_x < width as i32 && source_y >= 0 && source_y < height as i32
            {
                aligned_mask[y * output_width + x] =
                    foreground[source_y as usize * width + source_x as usize];
            }
            xx = xx.wrapping_add(a0);
            yy = yy.wrapping_add(a3);
        }
        a2 = a2.wrapping_add(a1);
        a5 = a5.wrapping_add(a4);
    }

    let mut aligned_rgb = Vec::with_capacity(output_width * output_height * 3);
    for y in 0..output_height {
        for x in 0..output_width {
            let output_x = x as f64 + 0.5;
            let output_y = y as f64 + 0.5;
            let source_x = inverse[0] * output_x + inverse[1] * output_y + inverse[2];
            let source_y = inverse[3] * output_x + inverse[4] * output_y + inverse[5];
            for channel in 0..3 {
                aligned_rgb.push(
                    bicubic_sample(&source_rgb, width, height, source_x, source_y, channel)
                        .unwrap_or(background[channel]),
                );
            }
        }
    }
    (aligned_rgb, aligned_mask, output_width, output_height)
}

struct RegionPreprocess {
    pixels: Vec<u8>,
    width: usize,
    height: usize,
    operation: String,
    confidence_ppm: i32,
    source_angle_milli_degrees: i32,
}

fn median_f64(values: &mut [f64]) -> f64 {
    values.sort_by(f64::total_cmp);
    let middle = values.len() / 2;
    if values.len() % 2 == 0 {
        (values[middle - 1] + values[middle]) / 2.0
    } else {
        values[middle]
    }
}

fn robust_line_fit(x: &[usize], y: &[usize]) -> Option<(f64, f64)> {
    if x.len() < 8 || x.len() != y.len() {
        return None;
    }
    let mut selected = vec![true; x.len()];
    let mut fit = (0.0, 0.0);
    for _ in 0..3 {
        let count = selected.iter().filter(|value| **value).count();
        if count < 8 {
            return None;
        }
        let count_f = count as f64;
        let mean_x = x
            .iter()
            .zip(&selected)
            .filter(|(_, keep)| **keep)
            .map(|(value, _)| *value as f64)
            .sum::<f64>()
            / count_f;
        let mean_y = y
            .iter()
            .zip(&selected)
            .filter(|(_, keep)| **keep)
            .map(|(value, _)| *value as f64)
            .sum::<f64>()
            / count_f;
        let numerator = x
            .iter()
            .zip(y)
            .zip(&selected)
            .filter(|(_, keep)| **keep)
            .map(|((x, y), _)| (*x as f64 - mean_x) * (*y as f64 - mean_y))
            .sum::<f64>();
        let denominator = x
            .iter()
            .zip(&selected)
            .filter(|(_, keep)| **keep)
            .map(|(value, _)| (*value as f64 - mean_x).powi(2))
            .sum::<f64>();
        if denominator <= f64::EPSILON {
            return None;
        }
        fit = (numerator / denominator, 0.0);
        fit.1 = mean_y - fit.0 * mean_x;
        let residuals: Vec<f64> = x
            .iter()
            .zip(y)
            .map(|(x, y)| (*y as f64 - (fit.0 * *x as f64 + fit.1)).abs())
            .collect();
        let mut selected_residuals: Vec<f64> = residuals
            .iter()
            .zip(&selected)
            .filter(|(_, keep)| **keep)
            .map(|(value, _)| *value)
            .collect();
        let scale = median_f64(&mut selected_residuals);
        let limit = 3.0_f64.max(scale * 3.0);
        selected = residuals.into_iter().map(|value| value <= limit).collect();
    }
    Some(fit)
}

fn detect_projector_quad(pixels: &[u8], width: usize, height: usize) -> Option<[(f64, f64); 4]> {
    if height < 220 || width < 220 {
        return None;
    }
    let mut field = vec![false; width * height];
    for y in 0..height {
        for x in 0..width {
            let offset = (y * width + x) * 3;
            let red = i16::from(pixels[offset]);
            let green = i16::from(pixels[offset + 1]);
            let blue = i16::from(pixels[offset + 2]);
            field[y * width + x] = green - red > 16 && blue - red > 28 && blue > 105;
        }
    }
    let window = 15_usize.max(61_usize.min((height / 40) | 1));
    let active_threshold = 3_usize.max((window as f64 * 0.25).round_ties_even() as usize);
    let half = window / 2;
    let mut columns = Vec::new();
    let mut first = vec![0_usize; width];
    let mut last = vec![0_usize; width];
    for x in 0..width {
        let mut prefix = vec![0_usize; height + 2 * half + 1];
        for padded_y in 0..height + 2 * half {
            let source_y = padded_y.checked_sub(half);
            let value = source_y
                .filter(|source_y| *source_y < height)
                .is_some_and(|source_y| field[source_y * width + x]);
            prefix[padded_y + 1] = prefix[padded_y] + usize::from(value);
        }
        let mut any = false;
        let mut first_active = 0;
        let mut last_active = 0;
        for y in 0..height {
            if prefix[y + window] - prefix[y] >= active_threshold {
                if !any {
                    first_active = y;
                    any = true;
                }
                last_active = y;
            }
        }
        if any && (last_active - first_active) as f64 >= height as f64 * 0.35 {
            first[x] = first_active;
            last[x] = last_active;
            columns.push(x);
        }
    }
    if (columns.len() as f64) < width as f64 * 0.35 {
        return None;
    }
    let left = *columns.first()?;
    let right = *columns.last()?;
    if ((right - left) as f64) < width as f64 * 0.45 {
        return None;
    }
    let top_values: Vec<usize> = columns.iter().map(|x| first[*x]).collect();
    let bottom_values: Vec<usize> = columns.iter().map(|x| last[*x]).collect();
    let (top_slope, top_intercept) = robust_line_fit(&columns, &top_values)?;
    let (bottom_slope, bottom_intercept) = robust_line_fit(&columns, &bottom_values)?;
    let inset = (window as f64 * 0.28).round_ties_even().max(2.0);
    let bounded_y = |value: f64| {
        value
            .round_ties_even()
            .clamp(0.0, height.saturating_sub(1) as f64)
    };
    let top_left = bounded_y(top_slope * left as f64 + top_intercept + inset);
    let top_right = bounded_y(top_slope * right as f64 + top_intercept + inset);
    let bottom_left = bounded_y(bottom_slope * left as f64 + bottom_intercept - inset);
    let bottom_right = bounded_y(bottom_slope * right as f64 + bottom_intercept - inset);
    if (bottom_left - top_left).min(bottom_right - top_right) < height as f64 * 0.25 {
        return None;
    }
    let margin = (width.min(height) as f64 * 0.025)
        .round_ties_even()
        .max(8.0);
    if left as f64 <= margin
        && right as f64 >= width.saturating_sub(1) as f64 - margin
        && top_left.max(top_right) <= margin
        && bottom_left.min(bottom_right) >= height.saturating_sub(1) as f64 - margin
    {
        return None;
    }
    Some([
        (left as f64, top_left),
        (right as f64, top_right),
        (right as f64, bottom_right),
        (left as f64, bottom_left),
    ])
}

fn solve_linear_8(mut matrix: [[f64; 9]; 8]) -> Option<[f64; 8]> {
    for column in 0..8 {
        let pivot = (column..8).max_by(|left, right| {
            matrix[*left][column]
                .abs()
                .total_cmp(&matrix[*right][column].abs())
        })?;
        if matrix[pivot][column].abs() < 1e-12 {
            return None;
        }
        matrix.swap(column, pivot);
        let divisor = matrix[column][column];
        for value in column..=8 {
            matrix[column][value] /= divisor;
        }
        for row in 0..8 {
            if row == column {
                continue;
            }
            let factor = matrix[row][column];
            for value in column..=8 {
                matrix[row][value] -= factor * matrix[column][value];
            }
        }
    }
    Some(std::array::from_fn(|index| matrix[index][8]))
}

fn perspective_coefficients(
    destination: [(f64, f64); 4],
    source: [(f64, f64); 4],
) -> Option<[f64; 8]> {
    let mut equations = [[0.0; 9]; 8];
    for (index, ((x, y), (u, v))) in destination.into_iter().zip(source).enumerate() {
        equations[index * 2] = [x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y, u];
        equations[index * 2 + 1] = [0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y, v];
    }
    solve_linear_8(equations)
}

fn perspective_warp(
    source: &[u8],
    source_width: usize,
    source_height: usize,
    output_width: usize,
    output_height: usize,
    coefficients: [f64; 8],
) -> Vec<u8> {
    let mut output = Vec::with_capacity(output_width * output_height * 3);
    for y in 0..output_height {
        for x in 0..output_width {
            let xin = x as f64 + 0.5;
            let yin = y as f64 + 0.5;
            let denominator = coefficients[6] * xin + coefficients[7] * yin + 1.0;
            let source_x =
                (coefficients[0] * xin + coefficients[1] * yin + coefficients[2]) / denominator;
            let source_y =
                (coefficients[3] * xin + coefficients[4] * yin + coefficients[5]) / denominator;
            for channel in 0..3 {
                output.push(
                    bicubic_sample(
                        source,
                        source_width,
                        source_height,
                        source_x,
                        source_y,
                        channel,
                    )
                    .unwrap_or(0),
                );
            }
        }
    }
    output
}

fn preprocess_projector_region(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> RegionPreprocess {
    let source = rgb_pixels(pixels, width, height, stride, channels);
    let identity = || RegionPreprocess {
        pixels: source.clone(),
        width,
        height,
        operation: "identity".to_string(),
        confidence_ppm: 1_000_000,
        source_angle_milli_degrees: 0,
    };
    if width.min(height) < 300 || width.saturating_mul(height) > 16_000_000 {
        return identity();
    }
    let Some(quad) = detect_projector_quad(&source, width, height) else {
        return identity();
    };
    let distance =
        |first: (f64, f64), second: (f64, f64)| (first.0 - second.0).hypot(first.1 - second.1);
    let top_width = distance(quad[0], quad[1]);
    let bottom_width = distance(quad[3], quad[2]);
    let left_height = distance(quad[0], quad[3]);
    let right_height = distance(quad[1], quad[2]);
    let output_width = ((top_width + bottom_width) / 2.0).round_ties_even() as usize;
    let output_height = ((left_height + right_height) / 2.0).round_ties_even() as usize;
    let polygon_area = (0..4)
        .map(|index| {
            quad[index].0 * quad[(index + 1) % 4].1 - quad[(index + 1) % 4].0 * quad[index].1
        })
        .sum::<f64>()
        .abs()
        / 2.0;
    let area_ratio = polygon_area / (width * height).max(1) as f64;
    let edge_balance = (top_width.min(bottom_width) / top_width.max(bottom_width).max(1.0))
        .min(left_height.min(right_height) / left_height.max(right_height).max(1.0));
    let confidence = (0.55 + 0.25 * (area_ratio / 0.5).min(1.0) + 0.20 * edge_balance).min(1.0);
    let top_angle = (quad[1].1 - quad[0].1)
        .atan2(quad[1].0 - quad[0].0)
        .to_degrees();
    let bottom_angle = (quad[2].1 - quad[3].1)
        .atan2(quad[2].0 - quad[3].0)
        .to_degrees();
    let source_angle = (top_angle + bottom_angle) / 2.0;
    let correction_magnitude = top_angle
        .abs()
        .max(bottom_angle.abs())
        .max((top_angle - bottom_angle).abs());
    let target_aspect = output_width as f64 / output_height.max(1) as f64;
    let source_aspect = width as f64 / height.max(1) as f64;
    if confidence < 0.82
        || correction_magnitude < 1.0
        || output_width.saturating_mul(output_height) > 16_000_000
        || (source_aspect <= 2.0 && target_aspect >= 2.8)
    {
        return identity();
    }
    let destination = [
        (0.0, 0.0),
        (output_width.saturating_sub(1) as f64, 0.0),
        (
            output_width.saturating_sub(1) as f64,
            output_height.saturating_sub(1) as f64,
        ),
        (0.0, output_height.saturating_sub(1) as f64),
    ];
    let Some(coefficients) = perspective_coefficients(destination, quad) else {
        return identity();
    };
    RegionPreprocess {
        pixels: perspective_warp(
            &source,
            width,
            height,
            output_width,
            output_height,
            coefficients,
        ),
        width: output_width,
        height: output_height,
        operation: "region-projector-dewarp".to_string(),
        confidence_ppm: (confidence * 1_000_000.0).round() as i32,
        source_angle_milli_degrees: (source_angle * 1_000.0).round() as i32,
    }
}

fn detect_physical_foreground(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
    background: [u8; 3],
) -> Vec<bool> {
    let mut exact_background = 0_usize;
    let mut differences = Vec::with_capacity(width * height);
    for y in 0..height {
        for x in 0..width {
            let color = rgb_at(pixels, stride, channels, x, y);
            let difference = color
                .iter()
                .zip(background)
                .map(|(value, base)| value.abs_diff(base))
                .max()
                .unwrap_or(0);
            exact_background += usize::from(difference == 0);
            differences.push(difference);
        }
    }
    let threshold = if exact_background * 100 >= width * height * 15 {
        0
    } else {
        let mut border = Vec::with_capacity(width * 2 + height * 2);
        border.extend_from_slice(&differences[..width]);
        border.extend_from_slice(&differences[(height - 1) * width..height * width]);
        border.extend((0..height).map(|y| differences[y * width]));
        border.extend((0..height).map(|y| differences[y * width + width - 1]));
        let mut ordered = border.clone();
        let median = f64::from(median_channel(&mut ordered));
        let mut deviations: Vec<u8> = border
            .iter()
            .map(|value| (f64::from(*value) - median).abs() as u8)
            .collect();
        let mad = f64::from(median_channel(&mut deviations));
        (median + 6.0 * mad + 2.0).clamp(2.0, 32.0) as u8
    };
    differences
        .into_iter()
        .map(|difference| difference > threshold)
        .collect()
}

fn rule_evidence_mask(
    pixels: &[u8],
    width: usize,
    height: usize,
    background: [u8; 3],
    foreground: &[bool],
    minimum_contrast: u8,
) -> Vec<bool> {
    if minimum_contrast == 0 {
        return foreground.to_vec();
    }
    let mut evidence = Vec::with_capacity(width * height);
    for y in 0..height {
        for x in 0..width {
            let offset = (y * width + x) * 3;
            let difference = pixels[offset..offset + 3]
                .iter()
                .zip(background)
                .map(|(value, base)| value.abs_diff(base))
                .max()
                .unwrap_or(0);
            evidence.push(foreground[y * width + x] && difference > minimum_contrast);
        }
    }
    evidence
}

fn rgb_at(pixels: &[u8], stride: usize, channels: usize, x: usize, y: usize) -> [u8; 3] {
    let offset = y * stride + x * channels;
    if channels == 1 {
        return [pixels[offset]; 3];
    }
    [pixels[offset], pixels[offset + 1], pixels[offset + 2]]
}

fn quantized_key(rgb: [u8; 3]) -> usize {
    usize::from(rgb[0] / 8) * 1024 + usize::from(rgb[1] / 8) * 32 + usize::from(rgb[2] / 8)
}

fn median_channel(values: &mut [u8]) -> u8 {
    values.sort_unstable();
    let middle = values.len() / 2;
    if values.len() % 2 == 0 {
        ((u16::from(values[middle - 1]) + u16::from(values[middle])) / 2) as u8
    } else {
        values[middle]
    }
}

fn modal_rgb<I>(colors: I) -> [u8; 3]
where
    I: IntoIterator<Item = [u8; 3]> + Clone,
{
    let mut histogram = vec![0_u32; 32_768];
    for color in colors.clone() {
        histogram[quantized_key(color)] += 1;
    }
    let modal_key = histogram
        .iter()
        .enumerate()
        .max_by_key(|(key, count)| (**count, std::cmp::Reverse(*key)))
        .map(|(key, _count)| key)
        .unwrap_or(0);
    let candidates: Vec<[u8; 3]> = colors
        .into_iter()
        .filter(|color| quantized_key(*color) == modal_key)
        .collect();
    if candidates.is_empty() {
        return [255; 3];
    }
    let mut red: Vec<u8> = candidates.iter().map(|color| color[0]).collect();
    let mut green: Vec<u8> = candidates.iter().map(|color| color[1]).collect();
    let mut blue: Vec<u8> = candidates.iter().map(|color| color[2]).collect();
    [
        median_channel(&mut red),
        median_channel(&mut green),
        median_channel(&mut blue),
    ]
}

fn estimate_background(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> [u8; 3] {
    let edge = rgb_at(pixels, stride, channels, 0, 0);
    let corners_match = [
        rgb_at(pixels, stride, channels, width - 1, 0),
        rgb_at(pixels, stride, channels, 0, height - 1),
        rgb_at(pixels, stride, channels, width - 1, height - 1),
    ]
    .iter()
    .all(|corner| *corner == edge);
    if corners_match {
        let uniform_row =
            |y: usize| (0..width).all(|x| rgb_at(pixels, stride, channels, x, y) == edge);
        let uniform_column =
            |x: usize| (0..height).all(|y| rgb_at(pixels, stride, channels, x, y) == edge);
        let top = (0..height).take_while(|y| uniform_row(*y)).count();
        let bottom = (0..height)
            .take_while(|offset| uniform_row(height - 1 - *offset))
            .count();
        let left = (0..width).take_while(|x| uniform_column(*x)).count();
        let right = (0..width)
            .take_while(|offset| uniform_column(width - 1 - *offset))
            .count();
        let thicknesses = [top, bottom, left, right];
        let mean_thickness = thicknesses.iter().sum::<usize>() as f64 / 4.0;
        let minimum = *thicknesses.iter().min().unwrap_or(&0);
        let maximum = *thicknesses.iter().max().unwrap_or(&0);
        let symmetric_frame =
            maximum.saturating_sub(minimum) <= (mean_thickness * 0.1).round().max(1.0) as usize;
        let narrow_frame = mean_thickness <= width.max(height) as f64 * 0.1;
        let same_edge_pixels = (0..height)
            .flat_map(|y| (0..width).map(move |x| (x, y)))
            .filter(|(x, y)| rgb_at(pixels, stride, channels, *x, *y) == edge)
            .count();
        let edge_ratio = same_edge_pixels as f64 / (width * height) as f64;
        if minimum >= 1
            && top + bottom < height
            && left + right < width
            && symmetric_frame
            && narrow_frame
            && edge_ratio >= 0.5
        {
            let inner_bottom = height - bottom;
            let inner_right = width - right;
            let ratio = |same: usize, total: usize| same as f64 / total.max(1) as f64;
            let boundary_ratios = [
                ratio(
                    (left..inner_right)
                        .filter(|x| rgb_at(pixels, stride, channels, *x, top) == edge)
                        .count(),
                    inner_right - left,
                ),
                ratio(
                    (left..inner_right)
                        .filter(|x| rgb_at(pixels, stride, channels, *x, inner_bottom - 1) == edge)
                        .count(),
                    inner_right - left,
                ),
                ratio(
                    (top..inner_bottom)
                        .filter(|y| rgb_at(pixels, stride, channels, left, *y) == edge)
                        .count(),
                    inner_bottom - top,
                ),
                ratio(
                    (top..inner_bottom)
                        .filter(|y| rgb_at(pixels, stride, channels, inner_right - 1, *y) == edge)
                        .count(),
                    inner_bottom - top,
                ),
            ];
            if boundary_ratios.iter().copied().fold(0.0_f64, f64::max) <= 0.5 {
                let edge_key = quantized_key(edge);
                let inner: Vec<[u8; 3]> = (top..inner_bottom)
                    .flat_map(|y| (left..inner_right).map(move |x| (x, y)))
                    .map(|(x, y)| rgb_at(pixels, stride, channels, x, y))
                    .filter(|color| quantized_key(*color) != edge_key)
                    .collect();
                if !inner.is_empty() {
                    return modal_rgb(inner);
                }
            }
        }
    }

    let inset_y = ((height as f64 * 0.2).round() as usize)
        .max(1)
        .min((height - 1) / 2);
    let inset_x = ((width as f64 * 0.2).round() as usize)
        .max(1)
        .min((width - 1) / 2);
    let colors: Vec<[u8; 3]> = (inset_y..height - inset_y)
        .flat_map(|y| (inset_x..width - inset_x).map(move |x| (x, y)))
        .map(|(x, y)| rgb_at(pixels, stride, channels, x, y))
        .collect();
    modal_rgb(colors)
}

pub fn analyze_foreground(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> Option<ForegroundGeometry> {
    if width == 0
        || height == 0
        || !matches!(channels, 1 | 3 | 4)
        || stride < width.checked_mul(channels)?
        || pixels.len() != stride.checked_mul(height)?
    {
        return None;
    }
    let region = preprocess_projector_region(pixels, width, height, stride, channels);
    let width = region.width;
    let height = region.height;
    let stride = width * 3;
    let background = estimate_background(&region.pixels, width, height, stride, 3);
    let source_physical =
        detect_physical_foreground(&region.pixels, width, height, stride, 3, background);
    let correction_milli_degrees = estimate_correction(&source_physical, width, height);
    let (pixels, warped_physical, aligned_width, aligned_height) = align_geometry(
        &region.pixels,
        width,
        height,
        stride,
        3,
        source_physical,
        background,
        correction_milli_degrees,
    );
    let mut physical_foreground = detect_physical_foreground(
        &pixels,
        aligned_width,
        aligned_height,
        aligned_width * 3,
        3,
        background,
    );
    for (physical, warped) in physical_foreground.iter_mut().zip(warped_physical) {
        *physical |= warped;
    }
    let rule_evidence = rule_evidence_mask(
        &pixels,
        aligned_width,
        aligned_height,
        background,
        &physical_foreground,
        32,
    );
    let (foreground, mode) = select_layout_foreground(
        &pixels,
        aligned_width,
        aligned_height,
        aligned_width * 3,
        3,
        &physical_foreground,
    );
    Some(ForegroundGeometry {
        width: aligned_width,
        height: aligned_height,
        stride: aligned_width * 3,
        pixels,
        background,
        physical_foreground,
        foreground,
        rule_evidence,
        mode,
        correction_milli_degrees,
        region_operation: region.operation,
        region_confidence_ppm: region.confidence_ppm,
        region_source_angle_milli_degrees: region.source_angle_milli_degrees,
    })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
enum RasterEdge {
    Bottom,
    Left,
    Right,
    Top,
}

#[derive(Clone, Debug)]
struct EdgeOpenCell {
    edge: RasterEdge,
    cross_coordinate: usize,
    span_start: usize,
    span_stop: usize,
    line_indexes: [usize; 3],
}

fn local_line_length(line: &LocalLine) -> usize {
    if line.horizontal {
        line.bbox[2] - line.bbox[0]
    } else {
        line.bbox[3] - line.bbox[1]
    }
}

fn local_line_thickness(line: &LocalLine) -> usize {
    if line.horizontal {
        line.bbox[3] - line.bbox[1]
    } else {
        line.bbox[2] - line.bbox[0]
    }
}

fn edge_open_grid_line_indexes(
    lines: &[LocalLine],
    width: usize,
    height: usize,
    tolerance: usize,
) -> Vec<usize> {
    let minimum_crossbar = 4 * 24;
    let maximum_thickness = (2 * tolerance + 1).max(3 * 4);
    let point_interval_gap = |point: usize, interval: (usize, usize)| {
        interval
            .0
            .saturating_sub(point)
            .max(point.saturating_sub(interval.1))
    };
    let mut candidates = Vec::<EdgeOpenCell>::new();
    for edge in [
        RasterEdge::Top,
        RasterEdge::Bottom,
        RasterEdge::Left,
        RasterEdge::Right,
    ] {
        let horizontal_crossbar = matches!(edge, RasterEdge::Top | RasterEdge::Bottom);
        let crossbars: Vec<usize> = lines
            .iter()
            .enumerate()
            .filter(|(_, line)| {
                line.horizontal == horizontal_crossbar
                    && local_line_length(line) >= minimum_crossbar
                    && local_line_thickness(line) <= maximum_thickness
            })
            .map(|(index, _)| index)
            .collect();
        let rails: Vec<usize> = lines
            .iter()
            .enumerate()
            .filter(|(_, line)| {
                line.horizontal != horizontal_crossbar
                    && local_line_thickness(line) <= maximum_thickness
            })
            .map(|(index, _)| index)
            .collect();
        for crossbar_index in crossbars {
            let crossbar = &lines[crossbar_index];
            let (coordinate, span) = if horizontal_crossbar {
                (
                    (crossbar.bbox[1] + crossbar.bbox[3]) / 2,
                    (crossbar.bbox[0], crossbar.bbox[2]),
                )
            } else {
                (
                    (crossbar.bbox[0] + crossbar.bbox[2]) / 2,
                    (crossbar.bbox[1], crossbar.bbox[3]),
                )
            };
            let minimum_rail = (2 * 24)
                .max((0.25 * local_line_length(crossbar) as f64).round_ties_even() as usize);
            let eligible: Vec<usize> = rails
                .iter()
                .copied()
                .filter(|index| {
                    let rail = &lines[*index];
                    if local_line_length(rail) < minimum_rail {
                        return false;
                    }
                    match edge {
                        RasterEdge::Bottom => {
                            rail.bbox[3] >= height.saturating_sub(tolerance)
                                && rail.bbox[1].abs_diff(coordinate) <= 2 * tolerance
                        }
                        RasterEdge::Top => {
                            rail.bbox[1] <= tolerance
                                && rail.bbox[3].abs_diff(coordinate) <= 2 * tolerance
                        }
                        RasterEdge::Right => {
                            rail.bbox[2] >= width.saturating_sub(tolerance)
                                && rail.bbox[0].abs_diff(coordinate) <= 2 * tolerance
                        }
                        RasterEdge::Left => {
                            rail.bbox[0] <= tolerance
                                && rail.bbox[2].abs_diff(coordinate) <= 2 * tolerance
                        }
                    }
                })
                .collect();
            let nearest = |endpoint: usize| {
                let mut values: Vec<usize> = eligible
                    .iter()
                    .copied()
                    .filter(|index| {
                        let rail = &lines[*index];
                        let interval = if horizontal_crossbar {
                            (rail.bbox[0], rail.bbox[2])
                        } else {
                            (rail.bbox[1], rail.bbox[3])
                        };
                        point_interval_gap(endpoint, interval) <= 2 * tolerance
                    })
                    .collect();
                values.sort_by_key(|index| {
                    let rail = &lines[*index];
                    let interval = if horizontal_crossbar {
                        (rail.bbox[0], rail.bbox[2])
                    } else {
                        (rail.bbox[1], rail.bbox[3])
                    };
                    (
                        point_interval_gap(endpoint, interval),
                        std::cmp::Reverse(local_line_length(rail)),
                        rail.bbox[1],
                        rail.bbox[0],
                        rail.bbox[3],
                        rail.bbox[2],
                    )
                });
                values
            };
            let first = nearest(span.0);
            let second = nearest(span.1);
            if first.is_empty() || second.is_empty() || first[0] == second[0] {
                continue;
            }
            candidates.push(EdgeOpenCell {
                edge,
                cross_coordinate: coordinate,
                span_start: span.0,
                span_stop: span.1,
                line_indexes: [crossbar_index, first[0], second[0]],
            });
        }
    }
    candidates.sort_by_key(|cell| {
        (
            cell.edge,
            cell.cross_coordinate,
            cell.span_start,
            cell.span_stop,
        )
    });
    let mut distinct = Vec::<EdgeOpenCell>::new();
    for cell in candidates {
        let duplicate = distinct.iter().any(|accepted| {
            if accepted.edge != cell.edge
                || accepted.cross_coordinate.abs_diff(cell.cross_coordinate) > 2 * tolerance
            {
                return false;
            }
            let overlap = accepted.span_stop.min(cell.span_stop) as isize
                - accepted.span_start.max(cell.span_start) as isize;
            let shorter =
                (accepted.span_stop - accepted.span_start).min(cell.span_stop - cell.span_start);
            overlap as f64 >= 0.75 * shorter as f64
        });
        if !duplicate {
            distinct.push(cell);
        }
    }
    let mut parents: Vec<usize> = (0..distinct.len()).collect();
    fn find(parents: &mut [usize], mut value: usize) -> usize {
        while parents[value] != value {
            parents[value] = parents[parents[value]];
            value = parents[value];
        }
        value
    }
    for first in 0..distinct.len() {
        for second in first + 1..distinct.len() {
            let left = &distinct[first];
            let right = &distinct[second];
            if left.edge == right.edge
                && left.cross_coordinate.abs_diff(right.cross_coordinate) <= 2 * tolerance
                && left
                    .span_start
                    .saturating_sub(right.span_stop)
                    .max(right.span_start.saturating_sub(left.span_stop))
                    <= 24
            {
                let first_root = find(&mut parents, first);
                let second_root = find(&mut parents, second);
                if first_root != second_root {
                    parents[second_root] = first_root;
                }
            }
        }
    }
    let mut families = std::collections::BTreeMap::<usize, Vec<usize>>::new();
    for index in 0..distinct.len() {
        let root = find(&mut parents, index);
        families.entry(root).or_default().push(index);
    }
    let mut selected = std::collections::BTreeSet::<usize>::new();
    for family in families.into_values().filter(|family| family.len() >= 3) {
        for cell_index in family {
            selected.extend(distinct[cell_index].line_indexes);
        }
    }
    let mut selected: Vec<usize> = selected.into_iter().collect();
    selected.sort_by_key(|index| {
        let line = &lines[*index];
        (
            !line.horizontal,
            line.bbox[1],
            line.bbox[0],
            line.bbox[3],
            line.bbox[2],
        )
    });
    selected
}

pub fn edge_open_grid_indexes(rgb: &[u8], width: usize, height: usize) -> Option<Vec<u32>> {
    let result = detect_local_structures_with_config(
        rgb,
        width,
        height,
        LocalStructureConfig {
            minimum_contrast: 8,
            minimum_length: 24,
            maximum_gap: 1,
            maximum_line_thickness: 4,
            junction_tolerance: 2,
        },
    )?;
    edge_open_grid_line_indexes(&result.lines, width, height, 4)
        .into_iter()
        .map(|index| u32::try_from(index).ok())
        .collect()
}

fn is_photographic_rule_region(rgb: &[u8], width: usize, height: usize, bbox: [usize; 4]) -> bool {
    let [left, top, right, bottom] = bbox;
    if left >= right || top >= bottom || right > width || bottom > height {
        return false;
    }
    let crop_width = right - left;
    let crop_height = bottom - top;
    let step = 1_usize.max(crop_width.max(crop_height).div_ceil(256));
    let sample_width = crop_width.div_ceil(step);
    let sample_height = crop_height.div_ceil(step);
    if sample_width.min(sample_height) < 8 {
        return false;
    }
    let mut sample = Vec::<[u8; 3]>::with_capacity(sample_width * sample_height);
    for y in (top..bottom).step_by(step) {
        for x in (left..right).step_by(step) {
            let offset = (y * width + x) * 3;
            sample.push([rgb[offset], rgb[offset + 1], rgb[offset + 2]]);
        }
    }
    let mut dense_gradients = 0_usize;
    for y in 0..sample_height {
        for x in 0..sample_width {
            let index = y * sample_width + x;
            let mut gradient = 0;
            if x > 0 {
                gradient = gradient.max(
                    sample[index]
                        .iter()
                        .zip(sample[index - 1])
                        .map(|(first, second)| first.abs_diff(second))
                        .max()
                        .unwrap_or(0),
                );
            }
            if y > 0 {
                gradient = gradient.max(
                    sample[index]
                        .iter()
                        .zip(sample[index - sample_width])
                        .map(|(first, second)| first.abs_diff(second))
                        .max()
                        .unwrap_or(0),
                );
            }
            dense_gradients += usize::from(gradient >= 32);
        }
    }
    let mut counts = [0_usize; 4096];
    for color in sample {
        let key = usize::from(color[0] / 16) * 256
            + usize::from(color[1] / 16) * 16
            + usize::from(color[2] / 16);
        counts[key] += 1;
    }
    let occupied: Vec<usize> = counts.into_iter().filter(|count| *count > 0).collect();
    let total: usize = occupied.iter().sum();
    let entropy = occupied
        .iter()
        .map(|count| {
            let probability = *count as f64 / total.max(1) as f64;
            -probability * probability.log2()
        })
        .sum::<f64>();
    occupied.len() >= 256
        && entropy >= 5.0
        && dense_gradients as f64 / (sample_width * sample_height) as f64 >= 0.12
}

#[derive(Clone)]
struct LocalNetworkDescriptor {
    network_index: usize,
    bbox: [usize; 4],
    horizontal: Vec<LocalLineBand>,
    vertical: Vec<LocalLineBand>,
}

fn describe_local_networks(
    rgb: &[u8],
    width: usize,
    height: usize,
    networks: &[LocalNetwork],
    tolerance: usize,
) -> Vec<LocalNetworkDescriptor> {
    networks
        .iter()
        .enumerate()
        .filter(|(_, network)| {
            network.bbox[2] - network.bbox[0] >= 4 * 24
                && network.bbox[3] - network.bbox[1] >= 4 * 24
                && !is_photographic_rule_region(rgb, width, height, network.bbox)
        })
        .map(|(network_index, network)| LocalNetworkDescriptor {
            network_index,
            bbox: network.bbox,
            horizontal: local_line_bands(network, true, tolerance),
            vertical: local_line_bands(network, false, tolerance),
        })
        .collect()
}

fn local_bands_cross(
    horizontal: &LocalLineBand,
    vertical: &LocalLineBand,
    tolerance: usize,
) -> bool {
    horizontal.members.iter().any(|horizontal_line| {
        vertical.members.iter().any(|vertical_line| {
            horizontal_line.bbox[0].saturating_sub(tolerance) <= vertical.coordinate
                && vertical.coordinate <= horizontal_line.bbox[2] + tolerance
                && vertical_line.bbox[1].saturating_sub(tolerance) <= horizontal.coordinate
                && horizontal.coordinate <= vertical_line.bbox[3] + tolerance
        })
    })
}

fn local_line_key(line: &LocalLine) -> (bool, [usize; 4], usize, u64) {
    (
        line.horizontal,
        line.bbox,
        line.support_pixels,
        line.strength.to_bits(),
    )
}

fn select_band_lines(
    selected: &mut std::collections::BTreeMap<(bool, [usize; 4], usize, u64), LocalLine>,
    bands: impl IntoIterator<Item = LocalLineBand>,
) {
    for band in bands {
        for line in band.members {
            selected.insert(local_line_key(&line), line);
        }
    }
}

pub fn local_structure_rule_drafts(
    rgb: &[u8],
    width: usize,
    height: usize,
) -> Option<Vec<RuleDraftSummary>> {
    let tolerance = 4;
    let detector = |contrast| {
        detect_local_structures_with_config(
            rgb,
            width,
            height,
            LocalStructureConfig {
                minimum_contrast: contrast,
                minimum_length: 24,
                maximum_gap: 1,
                maximum_line_thickness: 4,
                junction_tolerance: 2,
            },
        )
    };
    let primary = detector(8)?;
    let descriptors = describe_local_networks(rgb, width, height, &primary.networks, tolerance);
    let mut selected =
        std::collections::BTreeMap::<(bool, [usize; 4], usize, u64), LocalLine>::new();
    for index in edge_open_grid_line_indexes(&primary.lines, width, height, tolerance) {
        let line = primary.lines[index];
        selected.insert(local_line_key(&line), line);
    }
    let mut primary_cores =
        Vec::<(usize, [usize; 4], Vec<LocalLineBand>, Vec<LocalLineBand>)>::new();
    for core in &descriptors {
        let horizontal_candidates: Vec<LocalLineBand> = core
            .horizontal
            .iter()
            .filter(|band| band.coverage >= 0.55)
            .cloned()
            .collect();
        let vertical_candidates: Vec<LocalLineBand> = core
            .vertical
            .iter()
            .filter(|band| band.coverage >= 0.55)
            .cloned()
            .collect();
        let mut strong_horizontal: Vec<LocalLineBand> = horizontal_candidates
            .into_iter()
            .filter(|horizontal| {
                vertical_candidates
                    .iter()
                    .filter(|vertical| local_bands_cross(horizontal, vertical, tolerance))
                    .count()
                    >= 2
            })
            .collect();
        let strong_vertical: Vec<LocalLineBand> = vertical_candidates
            .into_iter()
            .filter(|vertical| {
                strong_horizontal
                    .iter()
                    .filter(|horizontal| local_bands_cross(horizontal, vertical, tolerance))
                    .count()
                    >= 2
            })
            .collect();
        strong_horizontal.retain(|horizontal| {
            strong_vertical
                .iter()
                .filter(|vertical| local_bands_cross(horizontal, vertical, tolerance))
                .count()
                >= 2
        });
        if strong_horizontal.len() < 3 || strong_vertical.len() < 3 {
            continue;
        }
        primary_cores.push((
            core.network_index,
            core.bbox,
            strong_horizontal.clone(),
            strong_vertical.clone(),
        ));
        select_band_lines(&mut selected, strong_horizontal.clone());
        select_band_lines(&mut selected, strong_vertical.clone());
        let core_height = core.bbox[3] - core.bbox[1];
        let core_coordinates: Vec<usize> = strong_horizontal
            .iter()
            .map(|band| band.coordinate)
            .collect();
        for flank in &descriptors {
            if flank.network_index == core.network_index {
                continue;
            }
            let overlap =
                core.bbox[3].min(flank.bbox[3]) as isize - core.bbox[1].max(flank.bbox[1]) as isize;
            if overlap <= 0 {
                continue;
            }
            let flank_height = flank.bbox[3] - flank.bbox[1];
            if overlap as f64 / (core_height.min(flank_height).max(1) as f64) < 0.75 {
                continue;
            }
            let horizontal_gap = core.bbox[0]
                .saturating_sub(flank.bbox[2])
                .max(flank.bbox[0].saturating_sub(core.bbox[2]));
            if horizontal_gap
                > 24_usize
                    .max((0.08 * core_height.max(flank_height) as f64).round_ties_even() as usize)
            {
                continue;
            }
            let mut repeated_horizontal: Vec<LocalLineBand> = flank
                .horizontal
                .iter()
                .filter(|band| {
                    band.coverage >= 0.55
                        && core_coordinates.iter().any(|coordinate| {
                            band.coordinate.abs_diff(*coordinate) <= 2 * tolerance
                        })
                })
                .cloned()
                .collect();
            let mut flank_vertical: Vec<LocalLineBand> = flank
                .vertical
                .iter()
                .filter(|band| band.coverage >= 0.45)
                .cloned()
                .collect();
            repeated_horizontal.retain(|horizontal| {
                flank_vertical
                    .iter()
                    .filter(|vertical| local_bands_cross(horizontal, vertical, tolerance))
                    .count()
                    >= 2
            });
            flank_vertical.retain(|vertical| {
                repeated_horizontal
                    .iter()
                    .filter(|horizontal| local_bands_cross(horizontal, vertical, tolerance))
                    .count()
                    >= 2
            });
            if repeated_horizontal.len() < 2 || flank_vertical.len() < 2 {
                continue;
            }
            select_band_lines(&mut selected, repeated_horizontal);
            select_band_lines(&mut selected, flank_vertical);
        }
    }
    if !primary_cores.is_empty() {
        let low = detector(3)?;
        let low_descriptors = describe_local_networks(rgb, width, height, &low.networks, tolerance);
        for low_network in low_descriptors {
            let low_width = low_network.bbox[2] - low_network.bbox[0];
            let low_vertical_support: Vec<LocalLineBand> = low_network
                .vertical
                .iter()
                .filter(|band| band.coverage >= 0.45)
                .cloned()
                .collect();
            let matches: Vec<usize> = primary_cores
                .iter()
                .enumerate()
                .filter(|(_, (_, bbox, _, vertical))| {
                    let overlap_width = bbox[2]
                        .min(low_network.bbox[2])
                        .saturating_sub(bbox[0].max(low_network.bbox[0]));
                    let shared = vertical
                        .iter()
                        .filter(|primary| {
                            low_vertical_support.iter().any(|low| {
                                primary.coordinate.abs_diff(low.coordinate) <= 2 * tolerance
                            })
                        })
                        .count();
                    let left_overhang = bbox[0].saturating_sub(low_network.bbox[0]);
                    let right_overhang = low_network.bbox[2].saturating_sub(bbox[2]);
                    overlap_width as f64 / low_width.max(1) as f64 >= 0.50
                        && shared >= 3
                        && left_overhang.max(right_overhang) <= 2 * tolerance
                        && (low_network.bbox[1] < bbox[1].saturating_sub(tolerance)
                            || low_network.bbox[3] > bbox[3] + tolerance)
                })
                .map(|(index, _)| index)
                .collect();
            if matches.len() != 1 {
                continue;
            }
            let (_, matched_bbox, core_horizontal, core_vertical) = &primary_cores[matches[0]];
            let shared_vertical: Vec<LocalLineBand> = low_vertical_support
                .into_iter()
                .filter(|low| {
                    core_vertical
                        .iter()
                        .any(|primary| low.coordinate.abs_diff(primary.coordinate) <= 2 * tolerance)
                })
                .collect();
            if shared_vertical.len() < 3 {
                continue;
            }
            let core_horizontal_coordinates: Vec<usize> =
                core_horizontal.iter().map(|band| band.coordinate).collect();
            let strong_low_horizontal: Vec<LocalLineBand> = low_network
                .horizontal
                .into_iter()
                .filter(|band| {
                    band.coverage >= 0.55
                        && (band.coordinate < matched_bbox[1].saturating_sub(tolerance)
                            || band.coordinate > matched_bbox[3] + tolerance
                            || core_horizontal_coordinates.iter().any(|coordinate| {
                                band.coordinate.abs_diff(*coordinate) <= 2 * tolerance
                            }))
                        && shared_vertical
                            .iter()
                            .filter(|vertical| local_bands_cross(band, vertical, tolerance))
                            .count()
                            >= 3
                })
                .collect();
            let supported_vertical: Vec<LocalLineBand> = shared_vertical
                .into_iter()
                .filter(|vertical| {
                    strong_low_horizontal
                        .iter()
                        .filter(|horizontal| local_bands_cross(horizontal, vertical, tolerance))
                        .count()
                        >= 3
                })
                .collect();
            let supported_horizontal: Vec<LocalLineBand> = strong_low_horizontal
                .into_iter()
                .filter(|horizontal| {
                    supported_vertical
                        .iter()
                        .filter(|vertical| local_bands_cross(horizontal, vertical, tolerance))
                        .count()
                        >= 3
                })
                .collect();
            if supported_horizontal.len() < 3 || supported_vertical.len() < 3 {
                continue;
            }
            for band in core_horizontal.iter().chain(core_vertical) {
                for line in &band.members {
                    selected.remove(&local_line_key(line));
                }
            }
            select_band_lines(&mut selected, supported_horizontal);
            select_band_lines(&mut selected, supported_vertical);
        }
    }
    let mut lines: Vec<LocalLine> = selected.into_values().collect();
    lines.sort_by_key(|line| {
        (
            !line.horizontal,
            line.bbox[1],
            line.bbox[0],
            line.bbox[3],
            line.bbox[2],
        )
    });
    Some(
        lines
            .into_iter()
            .map(|line| RuleDraftSummary {
                horizontal: line.horizontal,
                bbox: line.bbox,
            })
            .collect(),
    )
}

pub fn full_rule_drafts(
    mask: &[u8],
    rgb: &[u8],
    width: usize,
    height: usize,
) -> Option<Vec<RuleDraftSummary>> {
    let mut drafts = mask_rule_drafts(mask, width, height)?;
    drafts.extend(color_rule_drafts(rgb, width, height)?);
    let local = local_structure_rule_drafts(rgb, width, height)?;
    let tolerance = 4;
    for candidate in local {
        let covered = drafts.iter().any(|proven| {
            if proven.horizontal != candidate.horizontal {
                return false;
            }
            let (perpendicular_gap, contained) = if candidate.horizontal {
                (
                    proven.bbox[1]
                        .saturating_sub(candidate.bbox[3])
                        .max(candidate.bbox[1].saturating_sub(proven.bbox[3])),
                    proven.bbox[0].saturating_sub(tolerance) <= candidate.bbox[0]
                        && candidate.bbox[2] <= proven.bbox[2] + tolerance,
                )
            } else {
                (
                    proven.bbox[0]
                        .saturating_sub(candidate.bbox[2])
                        .max(candidate.bbox[0].saturating_sub(proven.bbox[2])),
                    proven.bbox[1].saturating_sub(tolerance) <= candidate.bbox[1]
                        && candidate.bbox[3] <= proven.bbox[3] + tolerance,
                )
            };
            perpendicular_gap <= tolerance && contained
        });
        if !covered {
            drafts.push(candidate);
        }
    }
    drafts.sort_by_key(|draft| {
        (
            !draft.horizontal,
            draft.bbox[1],
            draft.bbox[0],
            draft.bbox[3],
            draft.bbox[2],
        )
    });
    Some(drafts)
}

fn color_candidate_masks(
    rgb: &[u8],
    width: usize,
    height: usize,
) -> Option<(Arc<[u8]>, Arc<[u8]>)> {
    let mut horizontal_evidence = vec![0_u8; width * height];
    let maximum_offset = (4 + 1).min(1_usize.max((height - 1) / 2));
    for offset in 1..=maximum_offset {
        if height <= 2 * offset {
            break;
        }
        for y in offset..height - offset {
            for x in 0..width {
                let center = (y * width + x) * 3;
                let above = ((y - offset) * width + x) * 3;
                let below = ((y + offset) * width + x) * 3;
                let differs_above = (0..3)
                    .map(|channel| rgb[center + channel].abs_diff(rgb[above + channel]))
                    .max()
                    .unwrap_or(0)
                    >= 12;
                let differs_below = (0..3)
                    .map(|channel| rgb[center + channel].abs_diff(rgb[below + channel]))
                    .max()
                    .unwrap_or(0)
                    >= 12;
                let stable_flanks = (0..3)
                    .map(|channel| rgb[above + channel].abs_diff(rgb[below + channel]))
                    .max()
                    .unwrap_or(0)
                    <= 8;
                horizontal_evidence[y * width + x] |=
                    u8::from(differs_above && differs_below && stable_flanks);
            }
        }
    }
    horizontal_evidence = fill_short_gaps(&horizontal_evidence, width, height, true, 4)?;
    let horizontal = Arc::<[u8]>::from(orientation_candidates(
        &horizontal_evidence,
        width,
        height,
        true,
        width.min(24),
    )?);
    let mut vertical_evidence = vec![0_u8; width * height];
    for y in 0..height {
        for x in 1..width {
            let current = (y * width + x) * 3;
            vertical_evidence[y * width + x] = u8::from(
                (0..3)
                    .map(|channel| rgb[current + channel].abs_diff(rgb[current - 3 + channel]))
                    .max()
                    .unwrap_or(0)
                    >= 12,
            );
        }
    }
    vertical_evidence = fill_short_gaps(&vertical_evidence, width, height, false, 4)?;
    let vertical = Arc::<[u8]>::from(orientation_candidates(
        &vertical_evidence,
        width,
        height,
        false,
        height.min(24),
    )?);
    Some((horizontal, vertical))
}

fn full_rule_drafts_internal(
    mask: &[u8],
    rgb: &[u8],
    width: usize,
    height: usize,
) -> Option<Vec<MaskRuleDraft>> {
    let mut drafts = detect_mask_rule_drafts_internal(mask, width, height)?;
    let color = color_rule_drafts(rgb, width, height)?;
    if !color.is_empty() {
        let (horizontal_mask, vertical_mask) = color_candidate_masks(rgb, width, height)?;
        drafts.extend(color.into_iter().map(|summary| MaskRuleDraft {
            candidate_mask: if summary.horizontal {
                Arc::clone(&horizontal_mask)
            } else {
                Arc::clone(&vertical_mask)
            },
            summary,
            claim_full_bbox: false,
        }));
    }
    let local = local_structure_rule_drafts(rgb, width, height)?;
    let tolerance = 4;
    let mut local_candidate = vec![0_u8; width * height];
    for summary in &local {
        for y in summary.bbox[1]..summary.bbox[3] {
            local_candidate[y * width + summary.bbox[0]..y * width + summary.bbox[2]].fill(1);
        }
    }
    let local_candidate = Arc::<[u8]>::from(local_candidate);
    for summary in local {
        let covered = drafts.iter().any(|proven| {
            if proven.summary.horizontal != summary.horizontal {
                return false;
            }
            let (gap, contained) = if summary.horizontal {
                (
                    proven.summary.bbox[1]
                        .saturating_sub(summary.bbox[3])
                        .max(summary.bbox[1].saturating_sub(proven.summary.bbox[3])),
                    proven.summary.bbox[0].saturating_sub(tolerance) <= summary.bbox[0]
                        && summary.bbox[2] <= proven.summary.bbox[2] + tolerance,
                )
            } else {
                (
                    proven.summary.bbox[0]
                        .saturating_sub(summary.bbox[2])
                        .max(summary.bbox[0].saturating_sub(proven.summary.bbox[2])),
                    proven.summary.bbox[1].saturating_sub(tolerance) <= summary.bbox[1]
                        && summary.bbox[3] <= proven.summary.bbox[3] + tolerance,
                )
            };
            gap <= tolerance && contained
        });
        if !covered {
            drafts.push(MaskRuleDraft {
                summary,
                candidate_mask: Arc::clone(&local_candidate),
                claim_full_bbox: true,
            });
        }
    }
    drafts.sort_by_key(|draft| {
        (
            !draft.summary.horizontal,
            draft.summary.bbox[1],
            draft.summary.bbox[0],
            draft.summary.bbox[3],
            draft.summary.bbox[2],
        )
    });
    Some(drafts)
}

fn expand_rule_bands(
    drafts: Vec<MaskRuleDraft>,
    foreground: &[u8],
    width: usize,
    height: usize,
) -> Vec<MaskRuleDraft> {
    let maximum_halo = 4_usize.max((width.min(height) as f64 * 0.003).round_ties_even() as usize);
    let mut values = Vec::new();
    for mut draft in drafts {
        if draft.summary.horizontal {
            values.push(draft);
            continue;
        }
        let [left, top, right, bottom] = draft.summary.bbox;
        let search_left = left.saturating_sub(maximum_halo);
        let search_right = (right + maximum_halo).min(width);
        let minimum_pixels = ((bottom - top) as f64 * 0.8).ceil() as usize;
        let dense: Vec<usize> = (search_left..search_right)
            .filter(|x| {
                (top..bottom)
                    .filter(|y| foreground[y * width + *x] != 0)
                    .count()
                    >= minimum_pixels
            })
            .collect();
        if let (Some(first), Some(last)) = (dense.first(), dense.last()) {
            draft.summary.bbox = [
                search_left.max(first.saturating_sub(1)),
                top,
                search_right.min(last + 2),
                bottom,
            ];
        }
        draft.claim_full_bbox = true;
        values.push(draft);
    }
    let vertical: Vec<[usize; 4]> = values
        .iter()
        .filter(|draft| !draft.summary.horizontal)
        .map(|draft| draft.summary.bbox)
        .collect();
    for draft in &mut values {
        if !draft.summary.horizontal {
            continue;
        }
        let crossings: Vec<[usize; 4]> = vertical
            .iter()
            .copied()
            .filter(|bbox| {
                bbox[1] < draft.summary.bbox[3]
                    && draft.summary.bbox[1] < bbox[3]
                    && bbox[0] <= draft.summary.bbox[2]
                    && draft.summary.bbox[0] <= bbox[2]
            })
            .collect();
        if !crossings.is_empty() {
            draft.summary.bbox[0] = crossings
                .iter()
                .map(|bbox| bbox[0])
                .fold(draft.summary.bbox[0], usize::min);
            draft.summary.bbox[2] = crossings
                .iter()
                .map(|bbox| bbox[2])
                .fold(draft.summary.bbox[2], usize::max);
        }
        draft.claim_full_bbox = true;
    }
    values
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MaterializedRule {
    pub summary: RuleDraftSummary,
    pub foreground_pixels: usize,
    pub strength: f64,
}

pub fn materialize_candidate_rules(
    drafts: &[RuleCandidateDraft],
    foreground: &[u8],
    width: usize,
    height: usize,
) -> Option<(Vec<MaterializedRule>, Vec<u8>)> {
    if width == 0
        || height == 0
        || foreground.len() != width.checked_mul(height)?
        || drafts
            .iter()
            .any(|draft| draft.candidate_mask.len() != foreground.len())
    {
        return None;
    }
    let mut claimed = vec![0_u8; width * height];
    let mut rules = Vec::new();
    for draft in drafts {
        let [left, top, right, bottom] = draft.summary.bbox;
        if left >= right || top >= bottom || right > width || bottom > height {
            return None;
        }
        let mut available = 0;
        let mut eligible_count = 0;
        for y in top..bottom {
            for x in left..right {
                let index = y * width + x;
                let eligible = foreground[index] != 0
                    && (draft.claim_full_bbox || draft.candidate_mask[index] != 0);
                if eligible {
                    eligible_count += 1;
                    available += usize::from(claimed[index] == 0);
                }
            }
        }
        if available == 0 {
            continue;
        }
        for y in top..bottom {
            for x in left..right {
                let index = y * width + x;
                if foreground[index] != 0
                    && (draft.claim_full_bbox || draft.candidate_mask[index] != 0)
                {
                    claimed[index] = 1;
                }
            }
        }
        rules.push(MaterializedRule {
            summary: draft.summary,
            foreground_pixels: available,
            strength: eligible_count as f64 / ((right - left) * (bottom - top)) as f64,
        });
    }
    Some((rules, claimed))
}

pub fn materialize_full_rules_with_additions(
    rule_evidence: &[u8],
    rgb: &[u8],
    foreground: &[u8],
    width: usize,
    height: usize,
    additions: &[RuleCandidateDraft],
) -> Option<(Vec<MaterializedRule>, Vec<u8>)> {
    if foreground.len() != width.checked_mul(height)? {
        return None;
    }
    let mut base = full_rule_drafts_internal(rule_evidence, rgb, width, height)?;
    let suppressed = foreground.iter().filter(|value| **value != 0).count()
        - rule_evidence.iter().filter(|value| **value != 0).count();
    let evidence_pixels = rule_evidence.iter().filter(|value| **value != 0).count();
    if suppressed > evidence_pixels {
        base = expand_rule_bands(base, foreground, width, height);
    }
    let mut drafts: Vec<RuleCandidateDraft> = base
        .into_iter()
        .map(|draft| RuleCandidateDraft {
            summary: draft.summary,
            candidate_mask: draft.candidate_mask,
            claim_full_bbox: draft.claim_full_bbox,
            partition_evidence: true,
        })
        .collect();
    drafts.extend(additions.iter().cloned());
    drafts.sort_by_key(|draft| {
        (
            !draft.summary.horizontal,
            draft.summary.bbox[1],
            draft.summary.bbox[0],
            draft.summary.bbox[3],
            draft.summary.bbox[2],
        )
    });
    materialize_candidate_rules(&drafts, foreground, width, height)
}

pub fn materialize_full_rules(
    rule_evidence: &[u8],
    rgb: &[u8],
    foreground: &[u8],
    width: usize,
    height: usize,
) -> Option<(Vec<MaterializedRule>, Vec<u8>)> {
    materialize_full_rules_with_additions(rule_evidence, rgb, foreground, width, height, &[])
}

pub fn analyze_geometry(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> Option<GeometryAnalysis> {
    const MAXIMUM_RULE_THICKNESS: usize = 4;
    const MINIMUM_RULE_LENGTH: usize = 24;
    const MINIMUM_RULE_ASPECT_RATIO: f64 = 12.0;
    let config = PartitionConfig {
        max_nodes: 32_767,
        max_depth: 64,
        min_safe_gap: 3,
    };
    let foreground = analyze_foreground(pixels, width, height, stride, channels)?;
    let width = foreground.width;
    let height = foreground.height;
    let physical: Vec<u8> = foreground
        .physical_foreground
        .iter()
        .map(|value| u8::from(*value))
        .collect();
    let partition_foreground: Vec<u8> = foreground
        .foreground
        .iter()
        .map(|value| u8::from(*value))
        .collect();
    let rule_evidence: Vec<u8> = foreground
        .rule_evidence
        .iter()
        .map(|value| u8::from(*value))
        .collect();
    let adaptive = foreground.mode.starts_with("adaptive-");
    let mut additions = Vec::<RuleCandidateDraft>::new();
    let (mut rules, mut rule_mask) = materialize_full_rules_with_additions(
        &rule_evidence,
        &foreground.pixels,
        &physical,
        width,
        height,
        &additions,
    )?;
    let ownership_mask = |refined: &[u8], rules: &[u8]| {
        physical
            .iter()
            .zip(refined)
            .zip(rules)
            .map(|((physical, refined), rule)| {
                if adaptive {
                    u8::from(*refined != 0 || (*physical != 0 && *rule != 0))
                } else {
                    *physical
                }
            })
            .collect::<Vec<_>>()
    };
    let without_rules = |source: &[u8], rules: &[u8]| {
        source
            .iter()
            .zip(rules)
            .map(|(value, rule)| u8::from(*value != 0 && *rule == 0))
            .collect::<Vec<_>>()
    };
    let summaries =
        |rules: &[MaterializedRule]| rules.iter().map(|rule| rule.summary).collect::<Vec<_>>();
    let make_guard = |partition_mask: &[u8]| -> Option<Vec<u8>> {
        let guarded = guard_diacritic_gaps(partition_mask, width, height)?;
        Some(
            guarded
                .iter()
                .zip(partition_mask)
                .map(|(guard, partition)| u8::from(*guard != 0 && *partition == 0))
                .collect(),
        )
    };
    let mut ownership_foreground = ownership_mask(&partition_foreground, &rule_mask);
    let mut non_rule_foreground = without_rules(&ownership_foreground, &rule_mask);
    let mut connected = connected_component_runs(&non_rule_foreground, width, height)?;
    let mut partition_mask = without_rules(&partition_foreground, &rule_mask);
    let mut partition_components = connected_component_runs(&partition_mask, width, height)?;
    let mut diacritic_guard = make_guard(&partition_mask)?;
    let mut guard_stale = false;
    let mut row_grid = estimate_regular_row_grid(&partition_mask, width, height)?;
    let rule_summaries = summaries(&rules);
    let (mut partition_nodes, mut refined_partition_mask) = if adaptive {
        recursive_partition_with_adaptive_rgb(
            &partition_mask,
            width,
            height,
            &partition_components.0,
            &partition_components.1,
            config,
            &rule_summaries,
            Some(&diacritic_guard),
            row_grid,
            &[],
            true,
            &foreground.pixels,
            Some(&non_rule_foreground),
        )?
    } else {
        recursive_partition_without_adaptive_rgb(
            &partition_mask,
            width,
            height,
            &partition_components.0,
            &partition_components.1,
            config,
            &rule_summaries,
            Some(&diacritic_guard),
            row_grid,
            &[],
            false,
        )?
    };
    if adaptive {
        non_rule_foreground = without_rules(&refined_partition_mask, &rule_mask);
        connected = connected_component_runs(&non_rule_foreground, width, height)?;
        ownership_foreground = ownership_mask(&non_rule_foreground, &rule_mask);
    }
    let mut components =
        fragment_components_for_leaves(&connected.0, &connected.1, &partition_nodes)?;
    let residual_rule_drafts = recover_leaf_rule_networks(
        &rule_evidence,
        &non_rule_foreground,
        &rule_mask,
        width,
        height,
        &summaries(&rules),
        &partition_nodes,
        &components.0,
        &components.1,
        MAXIMUM_RULE_THICKNESS,
        MINIMUM_RULE_LENGTH,
        MINIMUM_RULE_ASPECT_RATIO,
    )?;
    let residual_mask: Arc<[u8]> = Arc::from(
        rule_evidence
            .iter()
            .zip(&rule_mask)
            .map(|(evidence, claimed)| u8::from(*evidence != 0 && *claimed == 0))
            .collect::<Vec<_>>(),
    );
    let unclaimed_mask: Arc<[u8]> = Arc::from(non_rule_foreground.clone());
    let mut recursive_additions = Vec::<RuleCandidateDraft>::new();
    let mut deferred_additions = Vec::<RuleCandidateDraft>::new();
    for draft in &residual_rule_drafts {
        let candidate = RuleCandidateDraft {
            summary: draft.summary,
            candidate_mask: if draft.partition_evidence {
                Arc::clone(&residual_mask)
            } else {
                Arc::clone(&unclaimed_mask)
            },
            claim_full_bbox: draft.claim_full_bbox,
            partition_evidence: draft.partition_evidence,
        };
        if draft.partition_evidence {
            recursive_additions.push(candidate);
        } else {
            deferred_additions.push(candidate);
        }
    }
    if !recursive_additions.is_empty() {
        let ownership_only_horizontal_boxes: Vec<[usize; 4]> = recursive_additions
            .iter()
            .filter(|draft| draft.summary.horizontal)
            .map(|draft| draft.summary.bbox)
            .collect();
        additions.extend(recursive_additions);
        (rules, rule_mask) = materialize_full_rules_with_additions(
            &rule_evidence,
            &foreground.pixels,
            &physical,
            width,
            height,
            &additions,
        )?;
        ownership_foreground = ownership_mask(&partition_foreground, &rule_mask);
        non_rule_foreground = without_rules(&ownership_foreground, &rule_mask);
        connected = connected_component_runs(&non_rule_foreground, width, height)?;
        partition_mask = without_rules(&partition_foreground, &rule_mask);
        partition_components = connected_component_runs(&partition_mask, width, height)?;
        diacritic_guard = make_guard(&partition_mask)?;
        guard_stale = false;
        row_grid = estimate_regular_row_grid(&partition_mask, width, height)?;
        let rule_summaries = summaries(&rules);
        (partition_nodes, refined_partition_mask) = if adaptive {
            recursive_partition_with_adaptive_rgb(
                &partition_mask,
                width,
                height,
                &partition_components.0,
                &partition_components.1,
                config,
                &rule_summaries,
                Some(&diacritic_guard),
                row_grid,
                &ownership_only_horizontal_boxes,
                true,
                &foreground.pixels,
                Some(&non_rule_foreground),
            )?
        } else {
            recursive_partition_without_adaptive_rgb(
                &partition_mask,
                width,
                height,
                &partition_components.0,
                &partition_components.1,
                config,
                &rule_summaries,
                Some(&diacritic_guard),
                row_grid,
                &ownership_only_horizontal_boxes,
                false,
            )?
        };
        if adaptive {
            non_rule_foreground = without_rules(&refined_partition_mask, &rule_mask);
            connected = connected_component_runs(&non_rule_foreground, width, height)?;
            ownership_foreground = ownership_mask(&non_rule_foreground, &rule_mask);
        }
        components = fragment_components_for_leaves(&connected.0, &connected.1, &partition_nodes)?;
    }
    if !deferred_additions.is_empty() {
        additions.extend(deferred_additions);
        (rules, rule_mask) = materialize_full_rules_with_additions(
            &rule_evidence,
            &foreground.pixels,
            &physical,
            width,
            height,
            &additions,
        )?;
        ownership_foreground = if adaptive {
            ownership_mask(&refined_partition_mask, &rule_mask)
        } else {
            physical.clone()
        };
        non_rule_foreground = without_rules(&ownership_foreground, &rule_mask);
        connected = connected_component_runs(&non_rule_foreground, width, height)?;
        guard_stale = true;
        components = fragment_components_for_leaves(&connected.0, &connected.1, &partition_nodes)?;
    }
    let mut final_line_count = 0_usize;
    for _ in 0..3 {
        let pass = recover_residual_line_drafts(
            &non_rule_foreground,
            &rule_evidence,
            width,
            height,
            &summaries(&rules),
            &components.0,
            MAXIMUM_RULE_THICKNESS,
            MINIMUM_RULE_LENGTH,
            MINIMUM_RULE_ASPECT_RATIO,
        )?;
        if pass.is_empty() {
            break;
        }
        final_line_count += pass.len();
        let candidate = Arc::<[u8]>::from(non_rule_foreground.clone());
        additions.extend(pass.into_iter().map(|draft| RuleCandidateDraft {
            summary: draft.summary,
            candidate_mask: Arc::clone(&candidate),
            claim_full_bbox: draft.claim_full_bbox,
            partition_evidence: draft.partition_evidence,
        }));
        (rules, rule_mask) = materialize_full_rules_with_additions(
            &rule_evidence,
            &foreground.pixels,
            &physical,
            width,
            height,
            &additions,
        )?;
        ownership_foreground = if adaptive {
            ownership_mask(&refined_partition_mask, &rule_mask)
        } else {
            physical.clone()
        };
        non_rule_foreground = without_rules(&ownership_foreground, &rule_mask);
        connected = connected_component_runs(&non_rule_foreground, width, height)?;
        guard_stale = true;
        components = fragment_components_for_leaves(&connected.0, &connected.1, &partition_nodes)?;
    }
    if guard_stale {
        partition_mask = without_rules(&partition_foreground, &rule_mask);
        diacritic_guard = make_guard(&partition_mask)?;
    }
    let component_ids: Vec<usize> = (0..components.0.len()).collect();
    partition_nodes = refine_component_boundaries(
        &partition_nodes,
        &components.0,
        &component_ids,
        &components.1,
        &diacritic_guard,
        width,
        height,
        config,
        &summaries(&rules),
    )?;
    let materialized_segments = materialize_segments(
        &components.0,
        &component_ids,
        &components.1,
        &partition_nodes,
        AffineBoxTransform {
            original_size: [width, height],
            inverse: [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        },
        width,
        height,
    )?;
    let materialized_nodes = materialize_nodes(
        &partition_nodes,
        &materialized_segments.leaf_segment_indexes,
        &materialized_segments.segments,
    )?;
    let sparse_matrix = project_sparse_matrix(
        width,
        height,
        &materialized_segments.ownership,
        materialized_segments.segments.len(),
        &summaries(&rules),
    )?;
    let owned_pixels: usize = materialized_segments
        .segments
        .iter()
        .map(|segment| segment.ink_pixels)
        .sum::<usize>()
        + rules
            .iter()
            .map(|rule| rule.foreground_pixels)
            .sum::<usize>();
    if owned_pixels
        != ownership_foreground
            .iter()
            .filter(|value| **value != 0)
            .count()
    {
        return None;
    }
    Some(GeometryAnalysis {
        foreground,
        ownership_foreground,
        rules,
        rule_mask,
        components: components.0,
        component_runs: components.1,
        partition_nodes,
        materialized_segments,
        materialized_nodes,
        sparse_matrix,
        recovered_rule_count: residual_rule_drafts.len() + final_line_count,
    })
}

fn percentile_usize(values: &[usize], percentile: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut values = values.to_vec();
    values.sort_unstable();
    let index = (values.len() - 1) as f64 * percentile / 100.0;
    let lower = index.floor() as usize;
    let upper = index.ceil() as usize;
    let fraction = index - lower as f64;
    values[lower] as f64 * (1.0 - fraction) + values[upper] as f64 * fraction
}

pub fn guard_diacritic_gaps(mask: &[u8], width: usize, height: usize) -> Option<Vec<u8>> {
    let components = connected_components(mask, width, height)?;
    let mut guarded = mask.to_vec();
    if components.len() < 2 {
        return Some(guarded);
    }
    let qualified: Vec<usize> = components
        .iter()
        .filter(|component| component.pixels >= 4 && component.bbox[3] - component.bbox[1] >= 3)
        .map(|component| component.bbox[3] - component.bbox[1])
        .collect();
    let search_heights: Vec<usize> = if qualified.is_empty() {
        components
            .iter()
            .map(|component| component.bbox[3] - component.bbox[1])
            .collect()
    } else {
        qualified
    };
    let maximum_gap =
        1_usize.max((percentile_usize(&search_heights, 95.0) * 0.5).round_ties_even() as usize);
    let bin_width = 64;
    let mut bins = std::collections::BTreeMap::<usize, Vec<usize>>::new();
    for (index, component) in components.iter().enumerate() {
        let first_bin = component.bbox[0] / bin_width;
        let last_bin = (component.bbox[2] - 1) / bin_width;
        for bin in first_bin..=last_bin {
            bins.entry(bin).or_default().push(index);
        }
    }
    for candidates in bins.values_mut() {
        candidates.sort_by_key(|index| (components[*index].bbox[1], components[*index].bbox[0]));
    }
    for upper in &components {
        let first_bin = upper.bbox[0] / bin_width;
        let last_bin = (upper.bbox[2] - 1) / bin_width;
        let mut candidates = std::collections::BTreeSet::<usize>::new();
        for bin in first_bin..=last_bin {
            let Some(bin_candidates) = bins.get(&bin) else {
                continue;
            };
            let start =
                bin_candidates.partition_point(|index| components[*index].bbox[1] < upper.bbox[3]);
            let stop = bin_candidates.partition_point(|index| {
                components[*index].bbox[1] < upper.bbox[3] + maximum_gap + 1
            });
            candidates.extend(bin_candidates[start..stop].iter().copied());
        }
        for index in candidates {
            let lower = &components[index];
            let gap = lower.bbox[1] as isize - upper.bbox[3] as isize;
            if gap <= 0 || gap as usize > maximum_gap {
                continue;
            }
            let lower_height = lower.bbox[3] - lower.bbox[1];
            if gap as usize > 1_usize.max((lower_height as f64 * 0.5).round_ties_even() as usize) {
                continue;
            }
            let overlap_left = upper.bbox[0].max(lower.bbox[0]);
            let overlap_right = upper.bbox[2].min(lower.bbox[2]);
            if overlap_right <= overlap_left {
                continue;
            }
            let upper_width = upper.bbox[2] - upper.bbox[0];
            let lower_width = lower.bbox[2] - lower.bbox[0];
            let upper_height = upper.bbox[3] - upper.bbox[1];
            let overlap_ratio =
                (overlap_right - overlap_left) as f64 / upper_width.min(lower_width) as f64;
            let upper_height_ratio = upper_height as f64 / lower_height as f64;
            let upper_pixel_ratio = upper.pixels as f64 / lower.pixels as f64;
            if overlap_ratio >= 0.5 && upper_height_ratio <= 0.5 && upper_pixel_ratio <= 0.55 {
                for y in upper.bbox[3]..lower.bbox[1] {
                    guarded[y * width + overlap_left..y * width + overlap_right].fill(1);
                }
            }
        }
    }
    Some(guarded)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn white_page_keeps_only_non_background_pixels() {
        let mut pixels = vec![255_u8; 4 * 4 * 3];
        pixels[(2 * 4 + 1) * 3..(2 * 4 + 1) * 3 + 3].fill(0);
        let result = analyze_foreground(&pixels, 4, 4, 12, 3).expect("valid image");
        assert_eq!(result.background, [255; 3]);
        assert_eq!(result.foreground_pixels(), 1);
        assert_eq!(result.mode, "physical-fallback");
    }

    #[test]
    fn connected_components_match_touching_run_contract() {
        let mask = [1, 0, 0, 0, 1, 0, 0, 0, 1];
        let components = connected_components(&mask, 3, 3).expect("valid mask");
        assert_eq!(
            components,
            vec![ConnectedComponent {
                bbox: [0, 0, 3, 3],
                pixels: 3,
            }]
        );
        let (_, runs) = connected_component_runs(&mask, 3, 3).expect("valid run mask");
        assert_eq!(
            runs,
            vec![
                ConnectedComponentRun {
                    component_index: 0,
                    row: 0,
                    start: 0,
                    stop: 1,
                },
                ConnectedComponentRun {
                    component_index: 0,
                    row: 1,
                    start: 1,
                    stop: 2,
                },
                ConnectedComponentRun {
                    component_index: 0,
                    row: 2,
                    start: 2,
                    stop: 3,
                },
            ]
        );
    }

    #[test]
    fn regular_row_grid_finds_symmetric_three_row_lattice() {
        let width = 12;
        let height = 36;
        let mut mask = vec![0_u8; width * height];
        for row_start in [3, 13, 23] {
            for y in row_start..row_start + 9 {
                for x in 2..10 {
                    mask[y * width + x] = 1;
                }
            }
        }
        let grid = estimate_regular_row_grid(&mask, width, height)
            .expect("valid mask")
            .expect("regular grid");
        assert_eq!(grid.pitch, 10);
        assert_eq!(grid.phase, 3);
        assert_eq!(grid.row_count, 3);
        assert_eq!(
            best_row_grid_boundary(&mask, width, height, [0, 0, width, height], Some(grid))
                .expect("valid boundary input"),
            Some(23)
        );
    }

    #[test]
    fn protected_upper_boundary_tracks_a_local_accent_bridge() {
        let width = 5;
        let height = 8;
        let mut mask = vec![0_u8; width * height];
        let mut guard = vec![0_u8; width * height];
        mask[2] = 1;
        mask[width + 2] = 1;
        for y in 2..4 {
            guard[y * width + 2] = 1;
        }
        assert_eq!(
            protected_upper_boundary(
                &mask,
                Some(&guard),
                width,
                height,
                [0, 0, width, height],
                3,
                4,
            ),
            Some(true)
        );
        for x in 0..width {
            mask[x] = 1;
            mask[width + x] = 1;
        }
        assert_eq!(
            protected_upper_boundary(
                &mask,
                Some(&guard),
                width,
                height,
                [0, 0, width, height],
                3,
                4,
            ),
            Some(false)
        );
    }

    #[test]
    fn body_evidence_requires_tall_components_on_both_sides() {
        let components = [
            ConnectedComponent {
                bbox: [1, 1, 5, 6],
                pixels: 20,
            },
            ConnectedComponent {
                bbox: [2, 10, 7, 16],
                pixels: 30,
            },
        ];
        assert_eq!(
            has_body_evidence_on_both_sides(&components, [0, 0, 8, 18], 8, 8.0),
            Some(true)
        );
        assert_eq!(
            has_body_evidence_on_both_sides(&components, [0, 0, 8, 9], 8, 8.0),
            Some(false)
        );
    }

    #[test]
    fn row_valley_selects_the_center_of_a_clean_interline_gap() {
        let width = 20;
        let height = 20;
        let mut mask = vec![0_u8; width * height];
        for y in 2..7 {
            mask[y * width + 2..y * width + 18].fill(1);
        }
        for y in 12..17 {
            mask[y * width + 2..y * width + 18].fill(1);
        }
        let components = connected_components(&mask, width, height).expect("valid mask");
        assert_eq!(
            best_row_valley(
                &mask,
                width,
                height,
                [0, 0, width, height],
                5.0,
                5.0,
                &components,
                None,
            ),
            Some(Some(10))
        );
    }

    #[test]
    fn imbalanced_row_gap_requires_a_structured_minority_row() {
        let components = [
            ConnectedComponent {
                bbox: [10, 30, 20, 35],
                pixels: 20,
            },
            ConnectedComponent {
                bbox: [22, 30, 32, 35],
                pixels: 20,
            },
        ];
        assert_eq!(
            supports_imbalanced_row_gap(&components, [0, 0, 100, 100], 40, 60, 10, 100),
            Some(true)
        );
        assert_eq!(
            supports_imbalanced_row_gap(&components[..1], [0, 0, 100, 100], 40, 60, 10, 100),
            Some(false)
        );
    }

    #[test]
    fn projected_column_gap_accepts_local_or_repeated_vertical_rules() {
        let nearby = RuleDraftSummary {
            horizontal: false,
            bbox: [49, 80, 51, 90],
        };
        let distant = RuleDraftSummary {
            horizontal: false,
            bbox: [49, 0, 51, 10],
        };
        let second_distant = RuleDraftSummary {
            horizontal: false,
            bbox: [50, 20, 52, 30],
        };
        assert_eq!(
            supports_projected_column_gap(&[nearby], [0, 100, 100, 120], 40, 60, 5.0),
            Some(true)
        );
        assert_eq!(
            supports_projected_column_gap(&[distant], [0, 100, 100, 120], 40, 60, 5.0),
            Some(false)
        );
        assert_eq!(
            supports_projected_column_gap(
                &[distant, second_distant],
                [0, 100, 100, 120],
                40,
                60,
                5.0,
            ),
            Some(true)
        );
    }

    #[test]
    fn best_separator_selects_the_clean_horizontal_gap() {
        let width = 20;
        let height = 20;
        let mut mask = vec![0_u8; width * height];
        for y in 2..7 {
            mask[y * width + 2..y * width + 18].fill(1);
        }
        for y in 12..17 {
            mask[y * width + 2..y * width + 18].fill(1);
        }
        let components = connected_components(&mask, width, height).expect("valid mask");
        assert_eq!(
            best_separator(
                &mask,
                width,
                height,
                [0, 0, width, height],
                2,
                2,
                &components,
                5.0,
                &[],
                None,
                true,
                &[],
                false,
            ),
            Some(Some(SeparatorChoice {
                rows: true,
                bbox: [0, 7, width, 12],
                rule_seam: false,
            }))
        );
    }

    #[test]
    fn layout_partition_evidence_removes_only_dust_components() {
        let width = 8;
        let height = 6;
        let mut mask = vec![0_u8; width * height];
        mask[0] = 1;
        for row in 2..5 {
            mask[row * width + 3..row * width + 6].fill(1);
        }
        let components = [
            ConnectedComponent {
                bbox: [0, 0, 1, 1],
                pixels: 1,
            },
            ConnectedComponent {
                bbox: [3, 2, 6, 5],
                pixels: 9,
            },
        ];
        let runs = [
            ConnectedComponentRun {
                component_index: 0,
                row: 0,
                start: 0,
                stop: 1,
            },
            ConnectedComponentRun {
                component_index: 1,
                row: 2,
                start: 3,
                stop: 6,
            },
            ConnectedComponentRun {
                component_index: 1,
                row: 3,
                start: 3,
                stop: 6,
            },
            ConnectedComponentRun {
                component_index: 1,
                row: 4,
                start: 3,
                stop: 6,
            },
        ];
        let (evidence, meaningful) =
            layout_partition_evidence(&mask, width, height, &components, &runs, 1)
                .expect("valid evidence input");
        assert_eq!(meaningful, vec![components[1]]);
        assert_eq!(evidence.iter().filter(|value| **value != 0).count(), 9);
        assert_eq!(evidence[0], 0);
    }

    #[test]
    fn base_recursive_partition_builds_two_atomic_row_leaves() {
        let width = 20;
        let height = 20;
        let mut mask = vec![0_u8; width * height];
        let mut runs = Vec::new();
        for (component_index, rows) in [(0, 2..7), (1, 12..17)] {
            for row in rows {
                mask[row * width + 2..row * width + 18].fill(1);
                runs.push(ConnectedComponentRun {
                    component_index,
                    row,
                    start: 2,
                    stop: 18,
                });
            }
        }
        let components = connected_components(&mask, width, height).expect("valid mask");
        let (nodes, working_mask) = recursive_partition_without_adaptive_rgb(
            &mask,
            width,
            height,
            &components,
            &runs,
            PartitionConfig {
                max_nodes: 100,
                max_depth: 10,
                min_safe_gap: 2,
            },
            &[],
            None,
            None,
            &[],
            false,
        )
        .expect("valid partition input");
        assert_eq!(working_mask, mask);
        assert_eq!(nodes.len(), 3);
        assert_eq!(nodes[0].rows, Some(true));
        assert_eq!(nodes[0].separator, Some([0, 7, width, 12]));
        assert_eq!(nodes[1].bbox, [0, 0, width, 7]);
        assert_eq!(nodes[2].bbox, [0, 12, width, height]);
        assert_eq!(nodes[1].stop_reason, Some(PartitionStopReason::Atomic));
        assert_eq!(nodes[2].stop_reason, Some(PartitionStopReason::Atomic));
    }

    #[test]
    fn forced_layout_foreground_keeps_an_empty_crop_empty() {
        let width = 12;
        let height = 8;
        let rgb = vec![255_u8; width * height * 3];
        let physical = vec![0_u8; width * height];
        let (selected, mode) = select_layout_foreground_mask(&rgb, width, height, &physical, true)
            .expect("valid crop");
        assert_eq!(mode, "adaptive-empty");
        assert!(selected.iter().all(|value| *value == 0));
    }

    #[test]
    fn leaf_fragmentation_splits_a_run_across_column_children() {
        let components = [ConnectedComponent {
            bbox: [2, 1, 8, 2],
            pixels: 6,
        }];
        let runs = [ConnectedComponentRun {
            component_index: 0,
            row: 1,
            start: 2,
            stop: 8,
        }];
        let leaf = |bbox, parent_index| PartitionNode {
            bbox,
            depth: 1,
            parent_index: Some(parent_index),
            rows: None,
            child_indexes: [None, None],
            separator: None,
            split_coordinate: None,
            stop_reason: Some(PartitionStopReason::Atomic),
            rule_partition: false,
            row_rule_top: false,
            row_rule_bottom: false,
        };
        let nodes = [
            PartitionNode {
                bbox: [0, 0, 10, 4],
                depth: 0,
                parent_index: None,
                rows: Some(false),
                child_indexes: [Some(1), Some(2)],
                separator: Some([5, 0, 5, 4]),
                split_coordinate: None,
                stop_reason: None,
                rule_partition: false,
                row_rule_top: false,
                row_rule_bottom: false,
            },
            leaf([0, 0, 5, 4], 0),
            leaf([5, 0, 10, 4], 0),
        ];
        let (fragments, fragment_runs) =
            fragment_components_for_leaves(&components, &runs, &nodes).expect("covered run");
        assert_eq!(
            fragments,
            vec![
                ConnectedComponent {
                    bbox: [2, 1, 5, 2],
                    pixels: 3,
                },
                ConnectedComponent {
                    bbox: [5, 1, 8, 2],
                    pixels: 3,
                },
            ]
        );
        assert_eq!(fragment_runs[0].component_index, 0);
        assert_eq!(fragment_runs[1].component_index, 1);
    }

    #[test]
    fn thin_line_axis_requires_collinear_structural_support() {
        let structural = [RuleDraftSummary {
            horizontal: true,
            bbox: [32, 10, 50, 12],
        }];
        assert_eq!(
            thin_line_axis([10, 10, 30, 12], 40, &structural, 4, 5),
            Some(Some(true))
        );
        assert_eq!(
            thin_line_axis([10, 20, 30, 22], 40, &structural, 4, 5),
            Some(None)
        );
    }

    #[test]
    fn horizontal_line_fallback_requires_two_upper_components() {
        let width = 30;
        let height = 20;
        let mut mask = vec![0_u8; width * height];
        mask[12 * width + 5..12 * width + 25].fill(1);
        for row in 8..10 {
            mask[row * width + 7..row * width + 10].fill(1);
            mask[row * width + 15..row * width + 18].fill(1);
        }
        let components = connected_components(&mask, width, height).expect("valid mask");
        let line_index = components
            .iter()
            .position(|component| component.bbox == [5, 12, 25, 13])
            .expect("line component");
        assert_eq!(
            unsupported_horizontal_line_component(
                line_index,
                &mask,
                width,
                height,
                &components,
                4.0,
                3,
                8,
                4.0,
            ),
            Some(true)
        );
        assert_eq!(
            component_has_rule_evidence(components[line_index], &mask, width, height),
            Some(true)
        );
    }

    #[test]
    fn residual_network_support_requires_crossing_rule_context() {
        let horizontal = ConnectedComponent {
            bbox: [0, 10, 20, 11],
            pixels: 20,
        };
        let vertical = ConnectedComponent {
            bbox: [10, 0, 11, 20],
            pixels: 20,
        };
        let vertical_bands = [
            RuleDraftSummary {
                horizontal: false,
                bbox: [0, 0, 1, 20],
            },
            RuleDraftSummary {
                horizontal: false,
                bbox: [19, 0, 20, 20],
            },
        ];
        let horizontal_bands = [
            RuleDraftSummary {
                horizontal: true,
                bbox: [0, 0, 20, 1],
            },
            RuleDraftSummary {
                horizontal: true,
                bbox: [0, 19, 20, 20],
            },
        ];
        assert_eq!(pure_vertical_line_component(vertical), Some(true));
        assert_eq!(
            horizontal_network_supported(horizontal, &vertical_bands, 2),
            Some(true)
        );
        assert_eq!(
            vertical_network_supported(0, &[vertical], &horizontal_bands, 2, 8),
            Some(true)
        );
    }

    #[test]
    fn residual_line_recovery_promotes_a_detached_underline() {
        let width = 30;
        let height = 20;
        let mut mask = vec![0_u8; width * height];
        mask[12 * width + 5..12 * width + 25].fill(1);
        for row in 8..10 {
            mask[row * width + 7..row * width + 10].fill(1);
            mask[row * width + 15..row * width + 18].fill(1);
        }
        let components = connected_components(&mask, width, height).expect("valid mask");
        let recovered =
            recover_residual_line_drafts(&mask, &mask, width, height, &[], &components, 3, 8, 4.0)
                .expect("valid recovery input");
        assert_eq!(recovered.len(), 1);
        assert_eq!(recovered[0].summary.bbox, [5, 12, 25, 13]);
        assert!(recovered[0].summary.horizontal);
    }

    #[test]
    fn leaf_rule_network_recovery_finds_crossbar_and_columns() {
        let width = 30;
        let height = 20;
        let mut evidence = vec![0_u8; width * height];
        evidence[10 * width..11 * width].fill(1);
        for row in 0..height {
            for column in [5, 15, 25] {
                evidence[row * width + column] = 1;
            }
        }
        let (components, runs) =
            connected_component_runs(&evidence, width, height).expect("valid network mask");
        let nodes = [PartitionNode {
            bbox: [0, 0, width, height],
            depth: 0,
            parent_index: None,
            rows: None,
            child_indexes: [None, None],
            separator: None,
            split_coordinate: None,
            stop_reason: Some(PartitionStopReason::Atomic),
            rule_partition: false,
            row_rule_top: false,
            row_rule_bottom: false,
        }];
        let recovered = recover_leaf_rule_networks(
            &evidence,
            &evidence,
            &vec![0_u8; evidence.len()],
            width,
            height,
            &[],
            &nodes,
            &components,
            &runs,
            3,
            8,
            4.0,
        )
        .expect("valid network input");
        assert_eq!(
            recovered
                .iter()
                .filter(|draft| draft.summary.horizontal)
                .count(),
            1
        );
        assert_eq!(
            recovered
                .iter()
                .filter(|draft| !draft.summary.horizontal)
                .count(),
            3
        );
    }

    #[test]
    fn component_boundary_splits_two_balanced_rows() {
        let components = [
            ConnectedComponent {
                bbox: [2, 2, 18, 6],
                pixels: 64,
            },
            ConnectedComponent {
                bbox: [2, 12, 18, 16],
                pixels: 64,
            },
        ];
        let runs = [
            ConnectedComponentRun {
                component_index: 0,
                row: 2,
                start: 2,
                stop: 18,
            },
            ConnectedComponentRun {
                component_index: 1,
                row: 12,
                start: 2,
                stop: 18,
            },
        ];
        let decision = best_component_boundary(
            &components,
            &[0, 1],
            &runs,
            [0, 0, 20, 20],
            &vec![0_u8; 400],
            20,
            20,
        )
        .expect("valid boundary input")
        .expect("row boundary");
        assert_eq!(decision.coordinate, 6);
        assert_eq!(decision.upper_indexes, vec![0]);
        assert_eq!(decision.lower_indexes, vec![1]);
    }

    #[test]
    fn rule_candidate_morphology_keeps_only_bounded_runs_and_gaps() {
        let mask = [1, 1, 0, 1, 1, 0, 0, 1];
        assert_eq!(
            fill_short_gaps(&mask, 8, 1, true, 1).expect("valid mask"),
            vec![1, 1, 1, 1, 1, 0, 0, 1]
        );
        assert_eq!(
            orientation_candidates(&mask, 8, 1, true, 2).expect("valid mask"),
            vec![1, 1, 0, 1, 1, 0, 0, 0]
        );
        assert!(!has_lateral_clearance(&mask, 8, 1, [0, 0, 2, 1], true));
    }

    #[test]
    fn edge_open_grid_selects_three_cells_without_drawing_missing_edge() {
        let line = |horizontal, bbox: [usize; 4]| LocalLine {
            horizontal,
            bbox,
            support_pixels: if horizontal {
                bbox[2] - bbox[0]
            } else {
                bbox[3] - bbox[1]
            },
            strength: 1.0,
        };
        let lines = vec![
            line(true, [0, 20, 100, 21]),
            line(true, [100, 20, 200, 21]),
            line(true, [200, 20, 300, 21]),
            line(false, [0, 20, 1, 100]),
            line(false, [100, 20, 101, 100]),
            line(false, [200, 20, 201, 100]),
            line(false, [300, 20, 301, 100]),
        ];
        let selected = edge_open_grid_line_indexes(&lines, 400, 100, 4);
        assert_eq!(selected.len(), 7);
        assert_eq!(
            selected
                .into_iter()
                .collect::<std::collections::BTreeSet<_>>(),
            (0..7).collect()
        );
    }
}
