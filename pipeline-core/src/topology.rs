use std::collections::{BTreeMap, BTreeSet};

use crate::geometry::{GeometryAnalysis, MaterializedSegment, SparseAxisInterval};
use crate::{MERGE_LEFT_CODE, MERGE_UP_CODE};

#[path = "topology_rules.rs"]
mod rules;
pub(crate) use rules::{RuledNetwork, rule_networks};

const TOPOLOGY_EMPTY_SLOT_CODE: u8 = 7;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SpatialValue {
    Empty,
    VisualRows(Vec<usize>),
    DenseRow(usize),
    RuledNetwork(usize),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SpatialSlot {
    pub start: usize,
    pub end: usize,
    pub value: SpatialValue,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PhysicalRow {
    pub source_rows: Vec<usize>,
    pub top: usize,
    pub bottom: usize,
    pub slots: Vec<SpatialSlot>,
    pub codes: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PhysicalTopology {
    pub rows: Vec<PhysicalRow>,
}

#[derive(Clone, Debug)]
struct VisualRow {
    row_index: usize,
    boxes: Vec<[usize; 4]>,
}

fn overlaps(first: (usize, usize), second: (usize, usize)) -> bool {
    first.0.max(second.0) < first.1.min(second.1)
}

fn visual_rows(segments: &[MaterializedSegment]) -> Vec<VisualRow> {
    let mut grouped = BTreeMap::<usize, Vec<[usize; 4]>>::new();
    for segment in segments {
        grouped
            .entry(segment.row_index)
            .or_default()
            .push(segment.bbox);
    }
    grouped
        .into_iter()
        .map(|(row_index, boxes)| VisualRow { row_index, boxes })
        .collect()
}

fn row_slots(
    row: usize,
    interval: SparseAxisInterval,
    columns: &[SparseAxisInterval],
    occupied_columns: &BTreeSet<usize>,
    vertical_rules: &[[usize; 4]],
    visual_rows: &[VisualRow],
    width: usize,
) -> Vec<SpatialSlot> {
    let active_rules: Vec<[usize; 4]> = vertical_rules
        .iter()
        .copied()
        .filter(|bbox| overlaps((interval.start, interval.end), (bbox[1], bbox[3])))
        .collect();
    let mut flow_intervals = Vec::<(usize, usize, usize)>::new();
    for visual_row in visual_rows {
        let fragments: Vec<(usize, usize)> = visual_row
            .boxes
            .iter()
            .filter(|bbox| overlaps((interval.start, interval.end), (bbox[1], bbox[3])))
            .map(|bbox| (bbox[0], bbox[2]))
            .collect();
        if let (Some(left), Some(right)) = (
            fragments.iter().map(|value| value.0).min(),
            fragments.iter().map(|value| value.1).max(),
        ) {
            flow_intervals.push((left, right, visual_row.row_index));
        }
    }
    let mut boundaries = BTreeSet::from([0_usize, width]);
    boundaries.extend(active_rules.iter().map(|bbox| (bbox[0] + bbox[2]) / 2));
    boundaries.extend(
        flow_intervals
            .iter()
            .flat_map(|(left, right, _)| [*left, *right]),
    );
    if flow_intervals.is_empty() && active_rules.is_empty() && !occupied_columns.is_empty() {
        if let Some(left) = occupied_columns
            .iter()
            .filter_map(|index| columns.get(*index).map(|value| value.start))
            .min()
        {
            boundaries.insert(left);
        }
        if let Some(right) = occupied_columns
            .iter()
            .filter_map(|index| columns.get(*index).map(|value| value.end))
            .max()
        {
            boundaries.insert(right);
        }
    }
    let ordered: Vec<usize> = boundaries.into_iter().collect();
    ordered
        .windows(2)
        .filter_map(|pair| {
            let (left, right) = (pair[0], pair[1]);
            (left < right).then(|| {
                let payload_rows: Vec<usize> = flow_intervals
                    .iter()
                    .filter(|(flow_left, flow_right, _)| {
                        overlaps((left, right), (*flow_left, *flow_right))
                    })
                    .map(|(_, _, row_index)| *row_index)
                    .collect();
                let fallback_payload = flow_intervals.is_empty()
                    && occupied_columns.iter().any(|column| {
                        columns
                            .get(*column)
                            .is_some_and(|value| overlaps((left, right), (value.start, value.end)))
                    });
                SpatialSlot {
                    start: left,
                    end: right,
                    value: if !payload_rows.is_empty() {
                        SpatialValue::VisualRows(payload_rows)
                    } else if fallback_payload {
                        SpatialValue::DenseRow(row)
                    } else {
                        SpatialValue::Empty
                    },
                }
            })
        })
        .collect()
}

fn encode_spatial_rows(rows: &[Vec<SpatialSlot>]) -> Vec<Vec<u8>> {
    let mut result = Vec::with_capacity(rows.len());
    let mut previous: &[SpatialSlot] = &[];
    for row in rows {
        let mut codes = Vec::with_capacity(row.len());
        for (column, slot) in row.iter().enumerate() {
            let empty = slot.value == SpatialValue::Empty;
            let merge_up = previous.iter().any(|above| {
                overlaps((slot.start, slot.end), (above.start, above.end))
                    && if empty {
                        above.value == SpatialValue::Empty
                    } else {
                        above.value != SpatialValue::Empty && slot.value == above.value
                    }
            });
            let merge_left = !empty
                && column > 0
                && row[column - 1].value != SpatialValue::Empty
                && slot.value == row[column - 1].value;
            codes.push(
                (if merge_up { MERGE_UP_CODE } else { 0 })
                    + (if merge_left { MERGE_LEFT_CODE } else { 0 })
                    + (if empty { TOPOLOGY_EMPTY_SLOT_CODE } else { 0 }),
            );
        }
        result.push(codes);
        previous = row;
    }
    result
}

pub fn build_physical_topology(analysis: &GeometryAnalysis) -> Option<PhysicalTopology> {
    let matrix = &analysis.sparse_matrix;
    let width = analysis.foreground.width;
    let mut cells_by_row = BTreeMap::<usize, BTreeSet<usize>>::new();
    for cell in &matrix.cells {
        if cell.row >= matrix.rows.len() || cell.column >= matrix.columns.len() {
            return None;
        }
        cells_by_row
            .entry(cell.row)
            .or_default()
            .insert(cell.column);
    }
    let horizontal_rule_rows: BTreeSet<usize> =
        matrix.horizontal_rule_rows.iter().copied().collect();
    let vertical_rules: Vec<[usize; 4]> = analysis
        .rules
        .iter()
        .filter(|rule| !rule.summary.horizontal)
        .map(|rule| rule.summary.bbox)
        .collect();
    let visual_rows = visual_rows(&analysis.materialized_segments.segments);
    let networks = rule_networks(analysis)?;
    let mut values = Vec::<PhysicalRow>::new();
    let mut rule_barrier = false;
    for (row, interval) in matrix.rows.iter().copied().enumerate() {
        if horizontal_rule_rows.contains(&row)
            || networks.iter().any(|n| {
                overlaps(
                    (interval.start, interval.end),
                    (n.y_lines[0], *n.y_lines.last().unwrap()),
                )
            })
        {
            rule_barrier = true;
            continue;
        }
        let slots = row_slots(
            row,
            interval,
            &matrix.columns,
            cells_by_row.get(&row).unwrap_or(&BTreeSet::new()),
            &vertical_rules,
            &visual_rows,
            width,
        );
        if let Some(previous) = values.last_mut()
            && !rule_barrier
            && previous.bottom == interval.start
            && previous.slots == slots
        {
            previous.source_rows.push(row);
            previous.bottom = interval.end;
        } else {
            values.push(PhysicalRow {
                source_rows: vec![row],
                top: interval.start,
                bottom: interval.end,
                slots,
                codes: Vec::new(),
            });
        }
        rule_barrier = false;
    }
    values.extend(rules::network_rows(analysis, &networks, &visual_rows)?);
    values.sort_by_key(|r| r.top);
    let mut group_start = 0_usize;
    for index in 1..=values.len() {
        let boundary = index == values.len() || values[index - 1].bottom != values[index].top;
        if !boundary {
            continue;
        }
        let slots: Vec<Vec<SpatialSlot>> = values[group_start..index]
            .iter()
            .map(|row| row.slots.clone())
            .collect();
        let encoded = encode_spatial_rows(&slots);
        for (row, codes) in values[group_start..index].iter_mut().zip(encoded) {
            row.codes = codes;
        }
        group_start = index;
    }
    Some(PhysicalTopology { rows: values })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spatial_codes_match_the_frozen_three_five_seven_alphabet() {
        let rows = vec![
            vec![
                SpatialSlot {
                    start: 0,
                    end: 5,
                    value: SpatialValue::VisualRows(vec![1]),
                },
                SpatialSlot {
                    start: 5,
                    end: 10,
                    value: SpatialValue::VisualRows(vec![1]),
                },
            ],
            vec![
                SpatialSlot {
                    start: 0,
                    end: 5,
                    value: SpatialValue::VisualRows(vec![1]),
                },
                SpatialSlot {
                    start: 5,
                    end: 10,
                    value: SpatialValue::Empty,
                },
            ],
        ];
        assert_eq!(encode_spatial_rows(&rows), vec![vec![0, 5], vec![3, 7]]);
    }
}
