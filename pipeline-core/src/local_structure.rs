use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct LocalLine {
    pub horizontal: bool,
    pub bbox: [usize; 4],
    pub support_pixels: usize,
    pub strength: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct LocalNetwork {
    pub bbox: [usize; 4],
    pub lines: Vec<LocalLine>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct LocalStructureResult {
    pub lines: Vec<LocalLine>,
    pub networks: Vec<LocalNetwork>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LocalStructureConfig {
    pub minimum_contrast: u8,
    pub minimum_length: usize,
    pub maximum_gap: usize,
    pub maximum_line_thickness: usize,
    pub junction_tolerance: usize,
}

impl Default for LocalStructureConfig {
    fn default() -> Self {
        Self {
            minimum_contrast: 8,
            minimum_length: 8,
            maximum_gap: 1,
            maximum_line_thickness: 4,
            junction_tolerance: 2,
        }
    }
}

#[derive(Clone, Copy)]
struct RunDraft {
    horizontal: bool,
    bbox: [usize; 4],
    support_pixels: usize,
    gradient_sum: usize,
}

struct DisjointSet {
    parents: Vec<usize>,
}

impl DisjointSet {
    fn new(size: usize) -> Self {
        Self {
            parents: (0..size).collect(),
        }
    }

    fn find(&mut self, mut value: usize) -> usize {
        while self.parents[value] != value {
            self.parents[value] = self.parents[self.parents[value]];
            value = self.parents[value];
        }
        value
    }

    fn union(&mut self, first: usize, second: usize) {
        let first_root = self.find(first);
        let second_root = self.find(second);
        if first_root != second_root {
            self.parents[second_root] = first_root;
        }
    }
}

pub fn detect_local_structures(
    rgb: &[u8],
    width: usize,
    height: usize,
) -> Option<LocalStructureResult> {
    detect_local_structures_with_config(rgb, width, height, LocalStructureConfig::default())
}

pub fn detect_local_structures_with_config(
    rgb: &[u8],
    width: usize,
    height: usize,
    config: LocalStructureConfig,
) -> Option<LocalStructureResult> {
    if width == 0 || height == 0 || rgb.len() != width.checked_mul(height)?.checked_mul(3)? {
        return None;
    }
    if height < 2 || width < 2 {
        return Some(LocalStructureResult {
            lines: Vec::new(),
            networks: Vec::new(),
        });
    }
    let mut horizontal_gradient = vec![0_u8; (height - 1) * width];
    for y in 0..height - 1 {
        for x in 0..width {
            let first = (y * width + x) * 3;
            let second = ((y + 1) * width + x) * 3;
            horizontal_gradient[y * width + x] = (0..3)
                .map(|channel| rgb[first + channel].abs_diff(rgb[second + channel]))
                .max()
                .unwrap_or(0);
        }
    }
    let mut vertical_gradient = vec![0_u8; (width - 1) * height];
    for x in 0..width - 1 {
        for y in 0..height {
            let first = (y * width + x) * 3;
            let second = first + 3;
            vertical_gradient[x * height + y] = (0..3)
                .map(|channel| rgb[first + channel].abs_diff(rgb[second + channel]))
                .max()
                .unwrap_or(0);
        }
    }
    let mut drafts = axis_run_drafts(
        &horizontal_gradient,
        width,
        true,
        config.minimum_contrast,
        config.minimum_length,
        config.maximum_gap,
    );
    drafts.extend(axis_run_drafts(
        &vertical_gradient,
        height,
        false,
        config.minimum_contrast,
        config.minimum_length,
        config.maximum_gap,
    ));
    let lines = materialize_lines(&drafts, config);
    let networks = group_networks(&lines, config);
    Some(LocalStructureResult { lines, networks })
}

fn axis_run_drafts(
    gradient: &[u8],
    line_length: usize,
    horizontal: bool,
    threshold: u8,
    minimum_length: usize,
    maximum_gap: usize,
) -> Vec<RunDraft> {
    let mut drafts = Vec::new();
    for (fixed_index, values) in gradient.chunks_exact(line_length).enumerate() {
        let indexes: Vec<usize> = values
            .iter()
            .enumerate()
            .filter(|(_, value)| **value >= threshold)
            .map(|(index, _)| index)
            .collect();
        if indexes.is_empty() {
            continue;
        }
        let mut runs = Vec::new();
        let mut start = indexes[0];
        let mut previous = start;
        for index in indexes.into_iter().skip(1) {
            if index - previous - 1 <= maximum_gap {
                previous = index;
            } else {
                runs.push((start, previous + 1));
                start = index;
                previous = index;
            }
        }
        runs.push((start, previous + 1));
        for (start, stop) in runs {
            if stop - start < minimum_length {
                continue;
            }
            let evidence: Vec<u8> = values[start..stop]
                .iter()
                .copied()
                .filter(|value| *value >= threshold)
                .collect();
            if evidence.is_empty() {
                continue;
            }
            let coordinate = fixed_index + 1;
            drafts.push(RunDraft {
                horizontal,
                bbox: if horizontal {
                    [start, coordinate, stop, coordinate + 1]
                } else {
                    [coordinate, start, coordinate + 1, stop]
                },
                support_pixels: evidence.len(),
                gradient_sum: evidence.into_iter().map(usize::from).sum(),
            });
        }
    }
    drafts
}

fn materialize_lines(drafts: &[RunDraft], config: LocalStructureConfig) -> Vec<LocalLine> {
    let mut disjoint = DisjointSet::new(drafts.len());
    for horizontal in [true, false] {
        let mut indexes: Vec<usize> = drafts
            .iter()
            .enumerate()
            .filter(|(_, draft)| draft.horizontal == horizontal)
            .map(|(index, _)| index)
            .collect();
        indexes.sort_by_key(|index| across_interval(drafts[*index].bbox, horizontal));
        let mut active = Vec::<usize>::new();
        for index in indexes {
            let current = drafts[index];
            let current_start = across_interval(current.bbox, horizontal).0;
            active.retain(|other| {
                across_interval(drafts[*other].bbox, horizontal).1 + config.maximum_line_thickness
                    >= current_start
            });
            for other in &active {
                if same_line_band(drafts[*other].bbox, current.bbox, horizontal, config) {
                    disjoint.union(*other, index);
                }
            }
            active.push(index);
        }
    }
    let mut groups = BTreeMap::<usize, Vec<RunDraft>>::new();
    for (index, draft) in drafts.iter().copied().enumerate() {
        let root = disjoint.find(index);
        groups.entry(root).or_default().push(draft);
    }
    let mut lines: Vec<LocalLine> = groups
        .into_values()
        .map(|members| {
            let support_pixels: usize = members.iter().map(|member| member.support_pixels).sum();
            LocalLine {
                horizontal: members[0].horizontal,
                bbox: union_boxes(members.iter().map(|member| member.bbox)),
                support_pixels,
                strength: (members
                    .iter()
                    .map(|member| member.gradient_sum)
                    .sum::<usize>() as f64
                    / (255 * support_pixels).max(1) as f64)
                    .min(1.0),
            }
        })
        .collect();
    lines.sort_by_key(line_order_key);
    lines
}

fn same_line_band(
    first: [usize; 4],
    second: [usize; 4],
    horizontal: bool,
    config: LocalStructureConfig,
) -> bool {
    let (first_along, second_along, first_across, second_across) = if horizontal {
        (
            (first[0], first[2]),
            (second[0], second[2]),
            (first[1], first[3]),
            (second[1], second[3]),
        )
    } else {
        (
            (first[1], first[3]),
            (second[1], second[3]),
            (first[0], first[2]),
            (second[0], second[2]),
        )
    };
    let endpoint_tolerance = config.maximum_gap + config.maximum_line_thickness;
    interval_gap(first_across, second_across) <= config.maximum_line_thickness
        && first_along.0.abs_diff(second_along.0) <= endpoint_tolerance
        && first_along.1.abs_diff(second_along.1) <= endpoint_tolerance
}

fn group_networks(lines: &[LocalLine], config: LocalStructureConfig) -> Vec<LocalNetwork> {
    let mut disjoint = DisjointSet::new(lines.len());
    let cell_size = 8_usize
        .max(config.minimum_length)
        .max(2 * (config.maximum_line_thickness + config.junction_tolerance) + 1);
    let mut buckets = BTreeMap::<(usize, usize), Vec<usize>>::new();
    for (index, line) in lines.iter().enumerate() {
        let keys = box_grid_keys(line.bbox, cell_size, config.junction_tolerance);
        let candidates: BTreeSet<usize> = keys
            .iter()
            .flat_map(|key| buckets.get(key).into_iter().flatten().copied())
            .collect();
        for other in candidates {
            if lines_touch(lines[other], *line, config.junction_tolerance) {
                disjoint.union(other, index);
            }
        }
        for key in keys {
            buckets.entry(key).or_default().push(index);
        }
    }
    let mut groups = BTreeMap::<usize, Vec<LocalLine>>::new();
    for (index, line) in lines.iter().copied().enumerate() {
        let root = disjoint.find(index);
        groups.entry(root).or_default().push(line);
    }
    let mut networks: Vec<LocalNetwork> = groups
        .into_values()
        .map(|mut members| {
            members.sort_by_key(line_order_key);
            LocalNetwork {
                bbox: union_boxes(members.iter().map(|line| line.bbox)),
                lines: members,
            }
        })
        .collect();
    networks.sort_by_key(|network| {
        (
            network.bbox[1],
            network.bbox[0],
            network.bbox[0],
            network.bbox[1],
            network.bbox[2],
            network.bbox[3],
        )
    });
    networks
}

fn lines_touch(first: LocalLine, second: LocalLine, tolerance: usize) -> bool {
    if first.horizontal != second.horizontal {
        let horizontal = if first.horizontal { first } else { second };
        let vertical = if first.horizontal { second } else { first };
        return interval_gap(
            (horizontal.bbox[0], horizontal.bbox[2]),
            (vertical.bbox[0], vertical.bbox[2]),
        ) <= tolerance
            && interval_gap(
                (horizontal.bbox[1], horizontal.bbox[3]),
                (vertical.bbox[1], vertical.bbox[3]),
            ) <= tolerance;
    }
    let (along, across) = if first.horizontal {
        (
            interval_gap(
                (first.bbox[0], first.bbox[2]),
                (second.bbox[0], second.bbox[2]),
            ),
            interval_gap(
                (first.bbox[1], first.bbox[3]),
                (second.bbox[1], second.bbox[3]),
            ),
        )
    } else {
        (
            interval_gap(
                (first.bbox[1], first.bbox[3]),
                (second.bbox[1], second.bbox[3]),
            ),
            interval_gap(
                (first.bbox[0], first.bbox[2]),
                (second.bbox[0], second.bbox[2]),
            ),
        )
    };
    along <= tolerance && across <= tolerance
}

fn interval_gap(first: (usize, usize), second: (usize, usize)) -> usize {
    if first.1 < second.0 {
        second.0 - first.1
    } else if second.1 < first.0 {
        first.0 - second.1
    } else {
        0
    }
}

fn across_interval(bbox: [usize; 4], horizontal: bool) -> (usize, usize) {
    if horizontal {
        (bbox[1], bbox[3])
    } else {
        (bbox[0], bbox[2])
    }
}

fn box_grid_keys(bbox: [usize; 4], cell_size: usize, padding: usize) -> Vec<(usize, usize)> {
    let left = bbox[0].saturating_sub(padding);
    let top = bbox[1].saturating_sub(padding);
    let right = bbox[2] + padding + 1;
    let bottom = bbox[3] + padding + 1;
    let mut keys = Vec::new();
    for row in top / cell_size..=(bottom - 1) / cell_size {
        for column in left / cell_size..=(right - 1) / cell_size {
            keys.push((row, column));
        }
    }
    keys
}

fn union_boxes(boxes: impl IntoIterator<Item = [usize; 4]>) -> [usize; 4] {
    let mut boxes = boxes.into_iter();
    let first = boxes.next().expect("non-empty local structure");
    boxes.fold(first, |union, bbox| {
        [
            union[0].min(bbox[0]),
            union[1].min(bbox[1]),
            union[2].max(bbox[2]),
            union[3].max(bbox[3]),
        ]
    })
}

fn line_order_key(line: &LocalLine) -> (bool, usize, usize, usize, usize) {
    (
        !line.horizontal,
        line.bbox[1],
        line.bbox[0],
        line.bbox[3],
        line.bbox[2],
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finite_cross_forms_one_local_network() {
        let mut rgb = vec![255; 12 * 12 * 3];
        for x in 2..10 {
            rgb[(6 * 12 + x) * 3..(6 * 12 + x) * 3 + 3].fill(0);
        }
        for y in 2..10 {
            rgb[(y * 12 + 6) * 3..(y * 12 + 6) * 3 + 3].fill(0);
        }
        let result = detect_local_structures(&rgb, 12, 12).expect("valid image");
        assert!(!result.lines.is_empty());
        assert_eq!(result.networks.len(), 1);
    }
}
