use std::collections::{BTreeMap, BTreeSet};

use crate::geometry::{
    GeometryAnalysis, MaterializedRule, MaterializedSegment, MaterializedSegmentSpan,
    SparseAxisInterval,
};
use crate::topology::{PhysicalTopology, SpatialValue};

const MAX_PAIRWISE_CHECKS: usize = 2_000_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ObjectKind {
    Paragraph,
    List,
    Table,
    Unknown,
}

#[derive(Clone, Debug, PartialEq)]
pub struct DocumentObject {
    pub kind: ObjectKind,
    pub segment_indexes: Vec<usize>,
    pub bbox: [usize; 4],
    pub reading_index: usize,
    pub row_start: usize,
    pub row_stop: usize,
    pub column_start: usize,
    pub column_stop: usize,
    pub confidence: f64,
    pub evidence: Vec<&'static str>,
    pub logical_spans: Option<Vec<MaterializedSegmentSpan>>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ObjectReconstruction {
    pub source_segment_indexes: Vec<usize>,
    pub objects: Vec<DocumentObject>,
    pub segment_ownership: Vec<usize>,
}

#[derive(Clone, Debug)]
struct ObjectDraft {
    kind: ObjectKind,
    segment_indexes: Vec<usize>,
    confidence: f64,
    evidence: Vec<&'static str>,
    sparse_span: Option<[usize; 4]>,
    bbox_override: Option<[usize; 4]>,
    logical_spans: Option<Vec<MaterializedSegmentSpan>>,
}

#[derive(Clone, Debug)]
struct ConnectivityDraft {
    segment_indexes: BTreeSet<usize>,
    bbox: [usize; 4],
}

#[derive(Clone, Debug)]
struct HorizontalRuleRegion {
    bbox: [usize; 4],
    rule_count: usize,
}

#[derive(Clone, Debug)]
struct TableCandidate {
    bbox: [usize; 4],
    members: Vec<usize>,
    sparse_span: [usize; 4],
    logical_cells: Vec<BTreeSet<(usize, usize)>>,
}

#[derive(Clone, Debug)]
struct DisjointSet {
    parent: Vec<usize>,
    rank: Vec<usize>,
}

impl DisjointSet {
    fn new(size: usize) -> Self {
        Self {
            parent: (0..size).collect(),
            rank: vec![0; size],
        }
    }

    fn find(&mut self, value: usize) -> usize {
        let mut parent = value;
        while self.parent[parent] != parent {
            parent = self.parent[parent];
        }
        let mut current = value;
        while self.parent[current] != current {
            let next = self.parent[current];
            self.parent[current] = parent;
            current = next;
        }
        parent
    }

    fn union(&mut self, first: usize, second: usize) {
        let mut first_root = self.find(first);
        let mut second_root = self.find(second);
        if first_root == second_root {
            return;
        }
        if self.rank[first_root] < self.rank[second_root] {
            std::mem::swap(&mut first_root, &mut second_root);
        }
        self.parent[second_root] = first_root;
        if self.rank[first_root] == self.rank[second_root] {
            self.rank[first_root] += 1;
        }
    }
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

fn boxes_intersect(first: [usize; 4], second: [usize; 4]) -> bool {
    first[0].max(second[0]) < first[2].min(second[2])
        && first[1].max(second[1]) < first[3].min(second[3])
}

fn median(mut values: Vec<f64>) -> f64 {
    values.sort_by(f64::total_cmp);
    let middle = values.len() / 2;
    if values.len() % 2 == 0 {
        (values[middle - 1] + values[middle]) / 2.0
    } else {
        values[middle]
    }
}

fn span_by_segment(analysis: &GeometryAnalysis) -> Option<Vec<MaterializedSegmentSpan>> {
    let segment_count = analysis.materialized_segments.segments.len();
    if analysis.sparse_matrix.spans.len() != segment_count {
        return None;
    }
    let mut values = vec![None; segment_count];
    for span in &analysis.sparse_matrix.spans {
        if span.segment_index >= segment_count || values[span.segment_index].is_some() {
            return None;
        }
        values[span.segment_index] = Some(*span);
    }
    values.into_iter().collect()
}

fn segment_key(
    index: usize,
    segments: &[MaterializedSegment],
    spans: &[MaterializedSegmentSpan],
) -> (usize, usize, usize, usize, usize, usize, usize) {
    let segment = &segments[index];
    let span = spans[index];
    (
        span.row_start,
        span.column_start,
        span.row_stop,
        span.column_stop,
        segment.bbox[1],
        segment.bbox[0],
        index,
    )
}

fn rule_bands(rules: &[(usize, &MaterializedRule)], horizontal: bool) -> Vec<(usize, usize)> {
    let mut intervals: Vec<(usize, usize)> = rules
        .iter()
        .map(|(_, rule)| {
            if horizontal {
                (rule.summary.bbox[1], rule.summary.bbox[3])
            } else {
                (rule.summary.bbox[0], rule.summary.bbox[2])
            }
        })
        .collect();
    intervals.sort_unstable();
    let mut merged: Vec<(usize, usize)> = Vec::new();
    for (start, stop) in intervals {
        if let Some(previous) = merged.last_mut()
            && start <= previous.1
        {
            previous.1 = previous.1.max(stop);
        } else {
            merged.push((start, stop));
        }
    }
    merged
}

fn logical_lane(interval: SparseAxisInterval, bands: &[(usize, usize)]) -> Option<usize> {
    let completed = bands.partition_point(|(_, stop)| *stop <= interval.start);
    if completed < 1 || completed >= bands.len() {
        return None;
    }
    let lane = completed - 1;
    (interval.start >= bands[lane].1 && interval.end <= bands[lane + 1].0).then_some(lane)
}

fn populated_table(members: &[usize], logical_cells: &[BTreeSet<(usize, usize)>]) -> bool {
    if members.len() < 3 {
        return false;
    }
    let occupied: BTreeSet<(usize, usize)> = members
        .iter()
        .flat_map(|index| logical_cells[*index].iter().copied())
        .collect();
    let rows: BTreeSet<usize> = occupied.iter().map(|(row, _)| *row).collect();
    let columns: BTreeSet<usize> = occupied.iter().map(|(_, column)| *column).collect();
    rows.len() >= 2 && columns.len() >= 2 && occupied.len() >= 3
}

fn table_drafts(
    analysis: &GeometryAnalysis,
    source_segments: &[usize],
    spans: &[MaterializedSegmentSpan],
) -> Option<(Vec<ObjectDraft>, Vec<bool>)> {
    let matrix = &analysis.sparse_matrix;
    let segment_count = analysis.materialized_segments.segments.len();
    if matrix.horizontal_rule_rows.is_empty() || matrix.vertical_rule_columns.is_empty() {
        return Some((Vec::new(), vec![false; segment_count]));
    }
    let mut horizontal: Vec<(usize, &MaterializedRule)> = analysis
        .rules
        .iter()
        .enumerate()
        .filter(|(_, rule)| rule.summary.horizontal)
        .collect();
    horizontal.sort_by_key(|(index, rule)| (rule.summary.bbox[1], rule.summary.bbox[0], *index));
    let mut vertical: Vec<(usize, &MaterializedRule)> = analysis
        .rules
        .iter()
        .enumerate()
        .filter(|(_, rule)| !rule.summary.horizontal)
        .collect();
    vertical.sort_by_key(|(index, rule)| (rule.summary.bbox[0], rule.summary.bbox[1], *index));
    if rule_bands(&horizontal, true).len() < 3 || rule_bands(&vertical, false).len() < 3 {
        return Some((Vec::new(), vec![false; segment_count]));
    }

    let mut combined = horizontal.clone();
    combined.extend(vertical.iter().copied());
    let vertical_offset = horizontal.len();
    let mut disjoint = DisjointSet::new(combined.len());
    let mut pairwise_checks = 0_usize;
    for (horizontal_index, (_, horizontal_rule)) in horizontal.iter().enumerate() {
        for (vertical_index, (_, vertical_rule)) in vertical.iter().enumerate() {
            pairwise_checks += 1;
            if pairwise_checks > MAX_PAIRWISE_CHECKS {
                return None;
            }
            if boxes_intersect(horizontal_rule.summary.bbox, vertical_rule.summary.bbox) {
                disjoint.union(horizontal_index, vertical_offset + vertical_index);
            }
        }
    }
    let mut networks: BTreeMap<usize, Vec<(usize, &MaterializedRule)>> = BTreeMap::new();
    for (index, rule) in combined.into_iter().enumerate() {
        let root = disjoint.find(index);
        networks.entry(root).or_default().push(rule);
    }
    let mut cells_by_segment = vec![Vec::<(usize, usize)>::new(); segment_count];
    for cell in &matrix.cells {
        if cell.segment_index >= segment_count {
            return None;
        }
        cells_by_segment[cell.segment_index].push((cell.row, cell.column));
    }
    let mut candidates = Vec::<TableCandidate>::new();
    for network in networks.into_values() {
        let network_horizontal: Vec<_> = network
            .iter()
            .copied()
            .filter(|(_, rule)| rule.summary.horizontal)
            .collect();
        let network_vertical: Vec<_> = network
            .iter()
            .copied()
            .filter(|(_, rule)| !rule.summary.horizontal)
            .collect();
        if rule_bands(&network_horizontal, true).len() < 3
            || rule_bands(&network_vertical, false).len() < 3
        {
            continue;
        }
        let network_bbox = bbox_union(network.iter().map(|(_, rule)| rule.summary.bbox))?;
        let page_frame_only = network_bbox
            == [0, 0, analysis.foreground.width, analysis.foreground.height]
            && !(network_horizontal.iter().any(|(_, rule)| {
                rule.summary.bbox[1] > 0 && rule.summary.bbox[3] < analysis.foreground.height
            }) && network_vertical.iter().any(|(_, rule)| {
                rule.summary.bbox[0] > 0 && rule.summary.bbox[2] < analysis.foreground.width
            }));
        if page_frame_only {
            continue;
        }
        let horizontal_bands = rule_bands(&network_horizontal, true);
        let vertical_bands = rule_bands(&network_vertical, false);
        let row_top = network_horizontal
            .iter()
            .map(|(_, rule)| rule.summary.bbox[1])
            .min()?;
        let row_bottom = network_horizontal
            .iter()
            .map(|(_, rule)| rule.summary.bbox[3])
            .max()?;
        let column_left = network_vertical
            .iter()
            .map(|(_, rule)| rule.summary.bbox[0])
            .min()?;
        let column_right = network_vertical
            .iter()
            .map(|(_, rule)| rule.summary.bbox[2])
            .max()?;
        let rows: Vec<usize> = matrix
            .rows
            .iter()
            .enumerate()
            .filter_map(|(index, interval)| {
                (interval.start < row_bottom && interval.end > row_top).then_some(index)
            })
            .collect();
        let columns: Vec<usize> = matrix
            .columns
            .iter()
            .enumerate()
            .filter_map(|(index, interval)| {
                (interval.start < column_right && interval.end > column_left).then_some(index)
            })
            .collect();
        let sparse_span = [
            *rows.iter().min()?,
            rows.iter().max()? + 1,
            *columns.iter().min()?,
            columns.iter().max()? + 1,
        ];
        let mut members = Vec::new();
        let mut logical_cells = vec![BTreeSet::<(usize, usize)>::new(); segment_count];
        for segment_index in source_segments {
            pairwise_checks += 1;
            if pairwise_checks > MAX_PAIRWISE_CHECKS {
                return None;
            }
            let span = spans[*segment_index];
            if span.row_start < sparse_span[0]
                || span.row_stop > sparse_span[1]
                || span.column_start < sparse_span[2]
                || span.column_stop > sparse_span[3]
            {
                continue;
            }
            for (row, column) in &cells_by_segment[*segment_index] {
                pairwise_checks += 1;
                if pairwise_checks > MAX_PAIRWISE_CHECKS {
                    return None;
                }
                let Some(logical_row) = logical_lane(matrix.rows[*row], &horizontal_bands) else {
                    continue;
                };
                let Some(logical_column) = logical_lane(matrix.columns[*column], &vertical_bands)
                else {
                    continue;
                };
                logical_cells[*segment_index].insert((logical_row, logical_column));
            }
            if !logical_cells[*segment_index].is_empty() {
                members.push(*segment_index);
            }
        }
        if populated_table(&members, &logical_cells) {
            candidates.push(TableCandidate {
                bbox: network_bbox,
                members,
                sparse_span,
                logical_cells,
            });
        }
    }
    candidates.sort_by_key(|candidate| candidate.bbox);
    let mut owned = vec![false; segment_count];
    let mut drafts = Vec::new();
    for candidate in candidates {
        let mut unowned: Vec<usize> = candidate
            .members
            .iter()
            .copied()
            .filter(|index| !owned[*index])
            .collect();
        if !populated_table(&unowned, &candidate.logical_cells) {
            continue;
        }
        unowned.sort_by_key(|index| {
            segment_key(*index, &analysis.materialized_segments.segments, spans)
        });
        for index in &unowned {
            owned[*index] = true;
        }
        let logical_spans = unowned
            .iter()
            .map(|index| {
                let cells = &candidate.logical_cells[*index];
                Some(MaterializedSegmentSpan {
                    segment_index: *index,
                    row_start: cells.iter().map(|(row, _)| *row).min()?,
                    row_stop: cells.iter().map(|(row, _)| *row).max()?.saturating_add(1),
                    column_start: cells.iter().map(|(_, column)| *column).min()?,
                    column_stop: cells
                        .iter()
                        .map(|(_, column)| *column)
                        .max()?
                        .saturating_add(1),
                })
            })
            .collect::<Option<Vec<_>>>()?;
        drafts.push(ObjectDraft {
            kind: ObjectKind::Table,
            segment_indexes: unowned,
            confidence: 1.0,
            evidence: vec!["orthogonal-rule-grid", "populated-logical-rule-grid"],
            sparse_span: Some(candidate.sparse_span),
            bbox_override: None,
            logical_spans: Some(logical_spans),
        });
    }
    Some((drafts, owned))
}

fn visual_rows(segment_indexes: &[usize], segments: &[MaterializedSegment]) -> Vec<Vec<usize>> {
    let mut ordered = segment_indexes.to_vec();
    ordered.sort_by_key(|index| {
        let bbox = segments[*index].bbox;
        (bbox[1], bbox[0], bbox[3], *index)
    });
    let mut rows: Vec<Vec<usize>> = Vec::new();
    for index in ordered {
        let candidate = segments[index].bbox;
        let same_row = rows.last().is_some_and(|row| {
            let row_center = median(
                row.iter()
                    .map(|item| {
                        let bbox = segments[*item].bbox;
                        (bbox[1] + bbox[3]) as f64 / 2.0
                    })
                    .collect(),
            );
            let row_height = median(
                row.iter()
                    .map(|item| {
                        let bbox = segments[*item].bbox;
                        (bbox[3] - bbox[1]) as f64
                    })
                    .collect(),
            );
            let candidate_center = (candidate[1] + candidate[3]) as f64 / 2.0;
            let candidate_height = (candidate[3] - candidate[1]) as f64;
            let smaller = row_height.min(candidate_height);
            let larger = row_height.max(candidate_height);
            let scale = if larger > 3.0 * smaller {
                smaller
            } else {
                larger
            };
            (row_center - candidate_center).abs() <= 0.65 * scale
        });
        if same_row {
            rows.last_mut().expect("row exists").push(index);
        } else {
            rows.push(vec![index]);
        }
    }
    for row in &mut rows {
        row.sort_by_key(|index| (segments[*index].bbox[0], *index));
    }
    rows
}

fn upper_quartile(mut values: Vec<usize>) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    values.sort_unstable();
    let index = (0.75 * (values.len().saturating_sub(1)) as f64).round() as usize;
    values[index] as f64
}

fn connectivity_row_link(
    first: &ConnectivityDraft,
    second: &ConnectivityDraft,
    typical_height: f64,
) -> bool {
    let (upper, lower) = if first.bbox[1] <= second.bbox[1] {
        (first.bbox, second.bbox)
    } else {
        (second.bbox, first.bbox)
    };
    let vertical_gap = lower[1].saturating_sub(upper[3]);
    let scale = typical_height.max(4.0);
    if vertical_gap as f64 > 12.0_f64.max(scale * 2.0) {
        return false;
    }
    let overlap = upper[2].min(lower[2]) as isize - upper[0].max(lower[0]) as isize;
    let aligned_left = upper[0].abs_diff(lower[0]) as f64 <= 4.0_f64.max(scale * 1.25);
    overlap > 0 || aligned_left
}

fn connectivity_column_link(
    first: &ConnectivityDraft,
    second: &ConnectivityDraft,
    typical_height: f64,
) -> bool {
    let (left, right) = if first.bbox[0] <= second.bbox[0] {
        (first.bbox, second.bbox)
    } else {
        (second.bbox, first.bbox)
    };
    let vertical_overlap = left[3].min(right[3]) as isize - left[1].max(right[1]) as isize;
    let horizontal_gap = right[0].saturating_sub(left[2]);
    vertical_overlap > 0 && horizontal_gap as f64 <= 12.0_f64.max(typical_height * 2.0)
}

fn has_empty_row_separator(
    analysis: &GeometryAnalysis,
    topology: &PhysicalTopology,
    node_index: usize,
    typical_height: f64,
    active_segments: &BTreeSet<usize>,
) -> bool {
    let node = &analysis.partition_nodes[node_index];
    if node.rows != Some(true) {
        return false;
    }
    let topology_heights: Vec<f64> = topology
        .rows
        .iter()
        .filter(|row| {
            row.bottom.saturating_sub(row.top) >= 3
                && row.top.max(node.bbox[1]) < row.bottom.min(node.bbox[3])
                && row
                    .slots
                    .iter()
                    .any(|slot| slot.value != SpatialValue::Empty)
        })
        .map(|row| row.bottom.saturating_sub(row.top) as f64)
        .collect();
    let local_scale = if topology_heights.is_empty() {
        typical_height
    } else {
        median(topology_heights)
    };
    let Some(separator) = node.separator else {
        return false;
    };
    let separator_height = separator[3].saturating_sub(separator[1]);
    if (separator_height as f64) < 8.0_f64.max(local_scale) {
        return false;
    }
    let edge_inset =
        1_usize.max((separator_height / 4).min(1_usize.max((local_scale * 0.25).round() as usize)));
    let core = [
        separator[0],
        separator[1].saturating_add(edge_inset),
        separator[2],
        separator[3].saturating_sub(edge_inset),
    ];
    core[1] < core[3]
        && !active_segments.iter().any(|index| {
            boxes_intersect(analysis.materialized_segments.segments[*index].bbox, core)
        })
}

fn merge_connectivity_drafts(
    drafts: Vec<ConnectivityDraft>,
    pairs: &[(usize, usize)],
) -> Option<Vec<ConnectivityDraft>> {
    let mut disjoint = DisjointSet::new(drafts.len());
    for (first, second) in pairs {
        disjoint.union(*first, *second);
    }
    let mut groups = BTreeMap::<usize, Vec<ConnectivityDraft>>::new();
    for (index, draft) in drafts.into_iter().enumerate() {
        groups.entry(disjoint.find(index)).or_default().push(draft);
    }
    groups
        .into_values()
        .map(|members| {
            let segment_indexes = members
                .iter()
                .flat_map(|member| member.segment_indexes.iter().copied())
                .collect();
            let bbox = bbox_union(members.iter().map(|member| member.bbox))?;
            Some(ConnectivityDraft {
                segment_indexes,
                bbox,
            })
        })
        .collect()
}

fn recurse_connectivity(
    analysis: &GeometryAnalysis,
    topology: &PhysicalTopology,
    node_segments: &[Vec<usize>],
    remaining: &[bool],
    node_index: usize,
) -> Option<Vec<ConnectivityDraft>> {
    let node = analysis.partition_nodes.get(node_index)?;
    let own: BTreeSet<usize> = node_segments
        .get(node_index)?
        .iter()
        .copied()
        .filter(|index| remaining.get(*index).copied().unwrap_or(false))
        .collect();
    if own.is_empty() {
        return Some(Vec::new());
    }
    let children: Vec<usize> = node.child_indexes.iter().flatten().copied().collect();
    if children.is_empty() {
        return Some(vec![ConnectivityDraft {
            bbox: bbox_union(
                own.iter()
                    .map(|index| analysis.materialized_segments.segments[*index].bbox),
            )?,
            segment_indexes: own,
        }]);
    }
    let mut drafts = Vec::new();
    let mut child_owners = Vec::new();
    for (owner, child_index) in children.iter().copied().enumerate() {
        for draft in
            recurse_connectivity(analysis, topology, node_segments, remaining, child_index)?
        {
            drafts.push(draft);
            child_owners.push(Some(owner));
        }
    }
    let covered: BTreeSet<usize> = drafts
        .iter()
        .flat_map(|draft| draft.segment_indexes.iter().copied())
        .collect();
    for index in own.difference(&covered).copied() {
        drafts.push(ConnectivityDraft {
            segment_indexes: BTreeSet::from([index]),
            bbox: analysis.materialized_segments.segments[index].bbox,
        });
        child_owners.push(None);
    }
    if drafts.len() < 2 {
        return Some(drafts);
    }
    let heights: Vec<usize> = own
        .iter()
        .map(|index| analysis.materialized_segments.segments[*index].bbox)
        .map(|bbox| bbox[3].saturating_sub(bbox[1]))
        .filter(|height| *height >= 3)
        .collect();
    let typical_height = if heights.is_empty() {
        4.0
    } else {
        upper_quartile(heights)
    };
    let short_row_scope = node.bbox[3].saturating_sub(node.bbox[1]) as f64
        <= 48.0_f64.max((typical_height * 3.0).min(96.0));
    let hard_row_separator =
        has_empty_row_separator(analysis, topology, node_index, typical_height, &own);
    let mut pairs = Vec::new();
    for first in 0..drafts.len() {
        for second in first + 1..drafts.len() {
            if child_owners[first].is_some() && child_owners[first] == child_owners[second] {
                continue;
            }
            if node.rows == Some(true)
                && !hard_row_separator
                && connectivity_row_link(&drafts[first], &drafts[second], typical_height)
            {
                pairs.push((first, second));
            } else if node.rows == Some(false)
                && short_row_scope
                && connectivity_column_link(&drafts[first], &drafts[second], typical_height)
            {
                pairs.push((first, second));
            }
        }
    }
    merge_connectivity_drafts(drafts, &pairs)
}

fn row_bbox(row: &[usize], segments: &[MaterializedSegment]) -> Option<[usize; 4]> {
    bbox_union(row.iter().map(|index| segments[*index].bbox))
}

fn marker_list_span(
    rows: &[Vec<usize>],
    segments: &[MaterializedSegment],
    body_height: f64,
) -> Option<(usize, usize)> {
    let mut witnesses = Vec::<(usize, [usize; 4], [usize; 4])>::new();
    for (index, row) in rows.iter().enumerate() {
        if row.len() < 2 {
            continue;
        }
        let marker = segments[row[0]].bbox;
        let body = bbox_union(row[1..].iter().map(|value| segments[*value].bbox))?;
        let marker_width = marker[2] - marker[0];
        let body_width = body[2] - body[0];
        let local_scale = median(
            row.iter()
                .map(|value| {
                    let bbox = segments[*value].bbox;
                    (bbox[3] - bbox[1]) as f64
                })
                .collect(),
        );
        if marker_width as f64 <= (2.0 * local_scale).max(body_width as f64 * 0.35)
            && body_width >= 2 * marker_width
            && marker[2] < body[0]
        {
            witnesses.push((index, marker, body));
        }
    }
    if witnesses.len() < 2 {
        return None;
    }
    let marker_left = median(witnesses.iter().map(|value| value.1[0] as f64).collect());
    let body_left = median(witnesses.iter().map(|value| value.2[0] as f64).collect());
    let tolerance = 4.0_f64.max(body_height * 0.35);
    let stable: Vec<_> = witnesses
        .into_iter()
        .filter(|value| {
            (value.1[0] as f64 - marker_left).abs() <= tolerance
                && (value.2[0] as f64 - body_left).abs() <= tolerance
        })
        .collect();
    if stable.len() < 2 {
        return None;
    }
    Some((
        stable.iter().map(|value| value.0).min()?,
        stable.iter().map(|value| value.0).max()? + 1,
    ))
}

fn split_flow_rows(
    rows: &[Vec<usize>],
    segments: &[MaterializedSegment],
    body_height: f64,
) -> Option<Vec<Vec<Vec<usize>>>> {
    if rows.len() < 2 {
        return Some(
            (!rows.is_empty())
                .then(|| vec![rows.to_vec()])
                .unwrap_or_default(),
        );
    }
    let row_boxes: Vec<[usize; 4]> = rows
        .iter()
        .map(|row| row_bbox(row, segments))
        .collect::<Option<_>>()?;
    let gaps: Vec<usize> = row_boxes
        .windows(2)
        .map(|pair| pair[1][1].saturating_sub(pair[0][3]))
        .collect();
    let mut positive: Vec<f64> = gaps
        .iter()
        .copied()
        .filter(|value| *value > 0)
        .map(|value| value as f64)
        .collect();
    positive.sort_by(f64::total_cmp);
    let ordinary_gap = if positive.is_empty() {
        0.0
    } else {
        let keep = 1_usize.max((positive.len() + 1) / 2);
        median(positive[..keep].to_vec())
    };
    let threshold = (body_height * 0.35)
        .max(ordinary_gap * 1.5)
        .max(ordinary_gap + 4.0);
    let mut result = vec![Vec::new()];
    for (index, row) in rows.iter().cloned().enumerate() {
        if index > 0 && gaps[index - 1] as f64 > threshold {
            result.push(Vec::new());
        }
        result.last_mut()?.push(row);
    }
    Some(
        result
            .into_iter()
            .filter(|group| !group.is_empty())
            .collect(),
    )
}

fn semantic_flow_drafts(
    connectivity: Vec<ConnectivityDraft>,
    segments: &[MaterializedSegment],
) -> Option<Vec<ObjectDraft>> {
    let mut result = Vec::new();
    for draft in connectivity {
        let indexes: Vec<usize> = draft.segment_indexes.iter().copied().collect();
        let rows = visual_rows(&indexes, segments);
        if rows.is_empty() {
            continue;
        }
        let heights: Vec<f64> = indexes
            .iter()
            .map(|index| segments[*index].bbox)
            .filter(|bbox| bbox[3] - bbox[1] >= 3 && bbox[2] - bbox[0] >= 4)
            .map(|bbox| (bbox[3] - bbox[1]) as f64)
            .collect();
        let body_height = if heights.is_empty() {
            4.0
        } else {
            median(heights)
        };
        let mut groups = Vec::<(bool, Vec<Vec<usize>>)>::new();
        if let Some((start, stop)) = marker_list_span(&rows, segments, body_height) {
            groups.extend(
                split_flow_rows(&rows[..start], segments, body_height)?
                    .into_iter()
                    .map(|group| (false, group)),
            );
            groups.push((true, rows[start..stop].to_vec()));
            groups.extend(
                split_flow_rows(&rows[stop..], segments, body_height)?
                    .into_iter()
                    .map(|group| (false, group)),
            );
        } else {
            groups.extend(
                split_flow_rows(&rows, segments, body_height)?
                    .into_iter()
                    .map(|group| (false, group)),
            );
        }
        for (is_list, group) in groups {
            let group_row_count = group.len();
            let mut segment_indexes: Vec<usize> = group.into_iter().flatten().collect();
            segment_indexes.sort_unstable();
            segment_indexes.dedup();
            let bbox = bbox_union(segment_indexes.iter().map(|index| segments[*index].bbox))?;
            result.push(ObjectDraft {
                kind: if is_list {
                    ObjectKind::List
                } else {
                    ObjectKind::Paragraph
                },
                segment_indexes,
                confidence: if is_list { 0.95 } else { 0.85 },
                evidence: if is_list {
                    vec!["repeated-marker-body-rows", "geometry-only-visual-rows"]
                } else if group_row_count >= 2 {
                    vec!["connected-multiline-flow", "geometry-only-visual-rows"]
                } else {
                    vec!["single-visual-row-paragraph", "geometry-only-visual-rows"]
                },
                sparse_span: None,
                bbox_override: Some(bbox),
                logical_spans: None,
            });
        }
    }
    Some(result)
}

fn horizontal_rule_regions(analysis: &GeometryAnalysis) -> Vec<HorizontalRuleRegion> {
    let page = [0, 0, analysis.foreground.width, analysis.foreground.height];
    let page_width = page[2] - page[0];
    let mut rules: Vec<[usize; 4]> = analysis
        .rules
        .iter()
        .filter(|rule| {
            rule.summary.horizontal
                && (rule.summary.bbox[2] - rule.summary.bbox[0]) as f64 >= page_width as f64 * 0.90
        })
        .map(|rule| rule.summary.bbox)
        .collect();
    rules.sort_by_key(|bbox| (bbox[1], bbox[0]));
    let mut groups = Vec::<Vec<[usize; 4]>>::new();
    for bbox in rules {
        let separate = groups.last().is_none_or(|group| {
            let previous = group.last().expect("rule group is non-empty");
            bbox[1].saturating_sub(previous[1]) > 96
                || bbox[0].abs_diff(previous[0]) > 2
                || bbox[2].abs_diff(previous[2]) > 2
        });
        if separate {
            groups.push(vec![bbox]);
        } else {
            groups.last_mut().expect("rule group exists").push(bbox);
        }
    }
    let mut result = Vec::new();
    for group in groups {
        if group.len() < 3 {
            continue;
        }
        let mut steps: Vec<usize> = group
            .windows(2)
            .map(|pair| pair[1][1].saturating_sub(pair[0][1]))
            .collect();
        steps.sort_unstable();
        let cadence = steps[steps.len() / 2];
        if cadence < 8
            || steps.last().unwrap() - steps.first().unwrap()
                > 3_usize.max((cadence as f64 * 0.20) as usize)
        {
            continue;
        }
        result.push(HorizontalRuleRegion {
            bbox: [
                group.iter().map(|bbox| bbox[0]).min().unwrap(),
                group[0][1].saturating_sub(cadence),
                group.iter().map(|bbox| bbox[2]).max().unwrap(),
                analysis
                    .foreground
                    .height
                    .min(group.last().unwrap()[3].saturating_add(cadence)),
            ],
            rule_count: group.len(),
        });
    }
    result
}

fn merge_horizontal_rule_tables(
    analysis: &GeometryAnalysis,
    mut drafts: Vec<ObjectDraft>,
) -> Option<Vec<ObjectDraft>> {
    let segments = &analysis.materialized_segments.segments;
    let page_width = analysis.foreground.width;
    for region in horizontal_rule_regions(analysis) {
        let selected: Vec<usize> = drafts
            .iter()
            .enumerate()
            .filter(|(_, draft)| matches!(draft.kind, ObjectKind::Paragraph | ObjectKind::Unknown))
            .filter(|(_, draft)| {
                let bbox = bbox_union(
                    draft
                        .segment_indexes
                        .iter()
                        .map(|index| segments[*index].bbox),
                )
                .expect("draft has segments");
                let center_y = (bbox[1] + bbox[3]) as f64 / 2.0;
                region.bbox[1] as f64 <= center_y
                    && center_y < region.bbox[3] as f64
                    && region.bbox[0].max(bbox[0]) < region.bbox[2].min(bbox[2])
            })
            .map(|(index, _)| index)
            .collect();
        if selected.len() < 2 {
            continue;
        }
        let selected_set: BTreeSet<usize> = selected.iter().copied().collect();
        let mut segment_indexes: Vec<usize> = selected
            .iter()
            .flat_map(|index| drafts[*index].segment_indexes.iter().copied())
            .collect();
        segment_indexes.sort_unstable();
        segment_indexes.dedup();
        let payload_bbox = bbox_union(segment_indexes.iter().map(|index| segments[*index].bbox))?;
        let rows = visual_rows(&segment_indexes, segments);
        if rows.len() < region.rule_count
            || rows.iter().any(|row| {
                row_bbox(row, segments).is_none_or(|bbox| bbox[2] - bbox[0] < page_width * 3 / 4)
            })
        {
            continue;
        }
        let table_bbox = bbox_union([payload_bbox, region.bbox])?;
        let first = selected[0];
        let merged = ObjectDraft {
            kind: ObjectKind::Table,
            segment_indexes,
            confidence: 1.0,
            evidence: vec!["stable-parallel-horizontal-rules"],
            sparse_span: None,
            bbox_override: Some(table_bbox),
            logical_spans: None,
        };
        drafts = drafts
            .into_iter()
            .enumerate()
            .filter_map(|(index, draft)| {
                if index == first {
                    Some(merged.clone())
                } else if selected_set.contains(&index) {
                    None
                } else {
                    Some(draft)
                }
            })
            .collect();
    }
    drafts.sort_by_key(|draft| {
        draft.bbox_override.unwrap_or_else(|| {
            bbox_union(
                draft
                    .segment_indexes
                    .iter()
                    .map(|index| segments[*index].bbox),
            )
            .expect("draft has segments")
        })
    });
    Some(drafts)
}

pub fn reconstruct_objects(analysis: &GeometryAnalysis) -> Option<ObjectReconstruction> {
    let topology = crate::topology::build_physical_topology(analysis)?;
    reconstruct_objects_with_topology(analysis, &topology)
}

pub fn reconstruct_objects_with_topology(
    analysis: &GeometryAnalysis,
    topology: &PhysicalTopology,
) -> Option<ObjectReconstruction> {
    let segments = &analysis.materialized_segments.segments;
    if segments.len() > 100_000
        || analysis.sparse_matrix.cells.len() > 1_000_000
        || analysis.rules.len() > 100_000
        || analysis.sparse_matrix.rows.len() + analysis.sparse_matrix.columns.len() > 1_000_000
    {
        return None;
    }
    let spans = span_by_segment(analysis)?;
    let mut source_segment_indexes: Vec<usize> = (0..segments.len()).collect();
    source_segment_indexes.sort_by_key(|index| segment_key(*index, segments, &spans));
    if source_segment_indexes.is_empty() {
        return Some(ObjectReconstruction {
            source_segment_indexes,
            objects: Vec::new(),
            segment_ownership: Vec::new(),
        });
    }
    let (mut drafts, table_owned) = table_drafts(analysis, &source_segment_indexes, &spans)?;
    let remaining: Vec<bool> = table_owned.iter().map(|owned| !owned).collect();
    let mut node_segments = vec![Vec::new(); analysis.partition_nodes.len()];
    for node in &analysis.materialized_nodes {
        if node.source_index >= node_segments.len() {
            return None;
        }
        node_segments[node.source_index] = node.segment_indexes.clone();
    }
    let connectivity = recurse_connectivity(analysis, topology, &node_segments, &remaining, 0)?;
    drafts.extend(semantic_flow_drafts(connectivity, segments)?);
    drafts = merge_horizontal_rule_tables(analysis, drafts)?;
    drafts.sort_by(|first, second| {
        let first_bbox = bbox_union(
            first
                .segment_indexes
                .iter()
                .map(|index| segments[*index].bbox),
        )
        .expect("draft has segments");
        let second_bbox = bbox_union(
            second
                .segment_indexes
                .iter()
                .map(|index| segments[*index].bbox),
        )
        .expect("draft has segments");
        (
            first_bbox[1],
            first_bbox[0],
            first_bbox[3],
            first_bbox[2],
            &first.segment_indexes,
        )
            .cmp(&(
                second_bbox[1],
                second_bbox[0],
                second_bbox[3],
                second_bbox[2],
                &second.segment_indexes,
            ))
    });
    if drafts.len() > 100_000 {
        return None;
    }
    let mut objects = Vec::with_capacity(drafts.len());
    for (reading_index, mut draft) in drafts.into_iter().enumerate() {
        draft
            .segment_indexes
            .sort_by_key(|index| segment_key(*index, segments, &spans));
        let bbox = draft.bbox_override.unwrap_or(bbox_union(
            draft
                .segment_indexes
                .iter()
                .map(|index| segments[*index].bbox),
        )?);
        let (row_start, row_stop, column_start, column_stop) = if let Some(span) = draft.sparse_span
        {
            if draft.segment_indexes.iter().any(|index| {
                let owned = spans[*index];
                owned.row_start < span[0]
                    || owned.row_stop > span[1]
                    || owned.column_start < span[2]
                    || owned.column_stop > span[3]
            }) {
                return None;
            }
            (span[0], span[1], span[2], span[3])
        } else {
            (
                draft
                    .segment_indexes
                    .iter()
                    .map(|index| spans[*index].row_start)
                    .min()?,
                draft
                    .segment_indexes
                    .iter()
                    .map(|index| spans[*index].row_stop)
                    .max()?,
                draft
                    .segment_indexes
                    .iter()
                    .map(|index| spans[*index].column_start)
                    .min()?,
                draft
                    .segment_indexes
                    .iter()
                    .map(|index| spans[*index].column_stop)
                    .max()?,
            )
        };
        objects.push(DocumentObject {
            kind: draft.kind,
            segment_indexes: draft.segment_indexes,
            bbox,
            reading_index,
            row_start,
            row_stop,
            column_start,
            column_stop,
            confidence: draft.confidence,
            evidence: draft.evidence,
            logical_spans: draft.logical_spans,
        });
    }
    let mut ownership_by_segment = vec![usize::MAX; segments.len()];
    for (object_index, object) in objects.iter().enumerate() {
        for segment_index in &object.segment_indexes {
            if ownership_by_segment[*segment_index] != usize::MAX {
                return None;
            }
            ownership_by_segment[*segment_index] = object_index;
        }
    }
    if ownership_by_segment
        .iter()
        .any(|value| *value == usize::MAX)
    {
        return None;
    }
    let segment_ownership = source_segment_indexes
        .iter()
        .map(|index| ownership_by_segment[*index])
        .collect();
    Some(ObjectReconstruction {
        source_segment_indexes,
        objects,
        segment_ownership,
    })
}
