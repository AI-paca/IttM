//! Object-local topology from the page matrix and the original geometry IDs.
//! The crop and logical lattice are distinct coordinate spaces. Empty slots are
//! retained even when they have no geometry member and require no OCR job.
use crate::geometry::{GeometryAnalysis, MaterializedSegmentSpan};
use crate::objects::DocumentObject;
use crate::topology::{
    PhysicalTopology, RuledNetwork, SpatialSlot, SpatialValue, encode_spatial_rows,
};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct LocalCell {
    pub bbox: [usize; 4],
    pub code: u8,
    pub segment_indexes: Vec<usize>,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct LocalRow {
    pub top: usize,
    pub bottom: usize,
    pub cells: Vec<LocalCell>,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ObjectLocalMatrix {
    pub rows: Vec<LocalRow>,
}
fn overlaps(a: (usize, usize), b: (usize, usize)) -> bool {
    a.0.max(b.0) < a.1.min(b.1)
}
fn intersects(a: [usize; 4], b: [usize; 4]) -> bool {
    overlaps((a[0], a[2]), (b[0], b[2])) && overlaps((a[1], a[3]), (b[1], b[3]))
}
fn contains(a: [usize; 4], b: [usize; 4]) -> bool {
    a[0] <= b[0] && a[1] <= b[1] && b[2] <= a[2] && b[3] <= a[3]
}
fn up(code: u8) -> bool {
    matches!(code, 3 | 8 | 10)
}
fn left(code: u8) -> bool {
    matches!(code, 5 | 8)
}
fn find(parents: &mut [usize], mut i: usize) -> usize {
    while parents[i] != i {
        parents[i] = parents[parents[i]];
        i = parents[i];
    }
    i
}
fn union(parents: &mut [usize], a: usize, b: usize) {
    let a = find(parents, a);
    let b = find(parents, b);
    parents[b] = a;
}

// Only physical edges from the saved matrix establish identity. In particular
// an upward slot that meets two independent parents must not join them.
fn payload_components(topology: &PhysicalTopology) -> BTreeMap<(usize, usize), usize> {
    let mut positions = BTreeMap::new();
    for (ri, row) in topology.rows.iter().enumerate() {
        for (ci, slot) in row.slots.iter().enumerate() {
            if slot.value != SpatialValue::Empty {
                positions.insert((ri, ci), positions.len());
            }
        }
    }
    let mut parents: Vec<_> = (0..positions.len()).collect();
    for (&(ri, ci), &current) in &positions {
        let row = &topology.rows[ri];
        if ci > 0 && left(row.codes[ci]) && row.slots[ci - 1].end == row.slots[ci].start {
            if let Some(&previous) = positions.get(&(ri, ci - 1)) {
                union(&mut parents, previous, current);
            }
        }
    }
    for (&(ri, ci), &current) in &positions {
        let row = &topology.rows[ri];
        if ri == 0 || !up(row.codes[ci]) || topology.rows[ri - 1].bottom != row.top {
            continue;
        }
        let slot = &row.slots[ci];
        let upper: Vec<_> = topology.rows[ri - 1]
            .slots
            .iter()
            .enumerate()
            .filter_map(|(i, s)| {
                overlaps((s.start, s.end), (slot.start, slot.end))
                    .then(|| positions.get(&(ri - 1, i)).copied())
                    .flatten()
            })
            .collect();
        let roots: BTreeSet<_> = upper.iter().map(|&i| find(&mut parents, i)).collect();
        if roots.len() == 1 {
            for i in upper {
                union(&mut parents, i, current);
            }
        }
    }
    positions
        .into_iter()
        .map(|(p, i)| (p, find(&mut parents, i)))
        .collect()
}

fn anchor_boundary(topology: &PhysicalTopology, network: &RuledNetwork) -> Option<usize> {
    let mut bands = BTreeMap::<(usize, usize), Vec<(usize, usize, bool, u8)>>::new();
    let top = *network.y_lines.first()?;
    let bottom = *network.y_lines.last()?;
    for row in &topology.rows {
        if row.top < top || row.bottom > bottom {
            continue;
        }
        let band = bands.entry((row.top, row.bottom)).or_default();
        band.extend(
            row.slots
                .iter()
                .zip(&row.codes)
                .map(|(s, &c)| (s.start, s.end, s.value != SpatialValue::Empty, c)),
        );
    }
    let mut local = Vec::new();
    for ((top, bottom), cells) in bands {
        let mut codes = Vec::new();
        for (column, x) in network.x_lines.windows(2).enumerate() {
            let overlapping: Vec<_> = cells
                .iter()
                .filter(|c| overlaps((x[0], x[1]), (c.0, c.1)))
                .collect();
            if overlapping.is_empty() {
                continue;
            }
            let payload: Vec<_> = overlapping.iter().copied().filter(|c| c.2).collect();
            let empty = payload.is_empty();
            let source = if empty { &overlapping } else { &payload };
            codes.push(
                u8::from(source.iter().any(|c| up(c.3))) * 3
                    + u8::from(column > 0 && source.iter().any(|c| left(c.3))) * 5
                    + u8::from(empty) * 7,
            );
        }
        if !codes.is_empty() {
            local.push((top, bottom, codes));
        }
    }
    for pair in local.windows(2) {
        let a = &pair[0];
        let b = &pair[1];
        if a.1 != b.0 || a.2.len() < 2 || b.2.len() < 2 {
            continue;
        }
        let first = a.2[0] - u8::from(up(a.2[0])) * 3;
        if first == 0 && left(a.2[1]) && up(b.2[0]) && up(b.2[1]) && left(b.2[1]) {
            return Some(a.1);
        }
    }
    None
}

pub(crate) fn build_local_matrix(
    analysis: &GeometryAnalysis,
    topology: &PhysicalTopology,
    networks: &[RuledNetwork],
    object: &DocumentObject,
) -> Option<ObjectLocalMatrix> {
    let [ml, mt, mr, mb] = object.matrix_bbox;
    if ml >= mr || mt >= mb {
        return None;
    }
    let sources: Vec<_> = object
        .segment_indexes
        .iter()
        .map(|&id| Some((id, analysis.materialized_segments.segments.get(id)?.bbox)))
        .collect::<Option<_>>()?;
    if topology.rows.iter().any(|r| r.codes.len() != r.slots.len()) {
        return None;
    }
    let network = if object.rule_lattice {
        let matches: Vec<_> = networks
            .iter()
            .filter(|n| {
                n.x_lines.first() == Some(&ml)
                    && n.x_lines.last() == Some(&mr)
                    && n.y_lines.first().is_some_and(|&y| y <= mt)
                    && n.y_lines.last().is_some_and(|&y| mb <= y)
                    && n.y_lines.contains(&mt)
                    && n.y_lines.contains(&mb)
            })
            .collect();
        if matches.len() != 1 {
            return None;
        }
        Some(matches[0])
    } else {
        None
    };
    let components = payload_components(topology);
    let embedded: BTreeSet<_> = sources
        .iter()
        .filter(|(_, b)| {
            topology.rows.iter().any(|r| {
                r.slots.iter().any(|s| {
                    s.value == SpatialValue::Empty
                        && contains([s.start, r.top, s.end, r.bottom], *b)
                })
            })
        })
        .map(|(id, _)| *id)
        .collect();
    let intervals: Vec<(usize, usize)> = if let Some(n) = network {
        let mut bounds: BTreeSet<_> = n
            .y_lines
            .iter()
            .copied()
            .filter(|&y| mt <= y && y <= mb)
            .collect();
        if bounds.len() == 2 {
            let candidates: BTreeSet<_> = topology
                .rows
                .iter()
                .flat_map(|r| [r.top, r.bottom])
                .chain(sources.iter().flat_map(|(_, b)| [b[1], b[3]]))
                .filter(|&y| mt < y && y < mb)
                .collect();
            let minimum = (n.x_lines.len() - 1).min(2.max((3 * (n.x_lines.len() - 1) + 4) / 5));
            let mut best: Option<(usize, usize, usize)> = None;
            for y in candidates {
                let above = n
                    .x_lines
                    .windows(2)
                    .filter(|x| {
                        sources
                            .iter()
                            .any(|(_, b)| intersects(*b, [x[0], mt, x[1], y]))
                    })
                    .count();
                let below = n
                    .x_lines
                    .windows(2)
                    .filter(|x| {
                        sources
                            .iter()
                            .any(|(_, b)| intersects(*b, [x[0], y, x[1], mb]))
                    })
                    .count();
                let support = above.min(below);
                let distance = (2 * y).abs_diff(mt + mb);
                if support < minimum {
                    continue;
                }
                if best.is_none_or(|(s, d, v)| {
                    support > s || support == s && (distance < d || distance == d && y > v)
                }) {
                    best = Some((support, distance, y));
                }
            }
            if let Some((_, _, y)) = best {
                bounds.insert(y);
            } else if let Some(y) = anchor_boundary(topology, n) {
                if mt < y && y < mb {
                    bounds.insert(y);
                }
            }
        }
        let ordered: Vec<_> = bounds.into_iter().collect();
        ordered.windows(2).map(|y| (y[0], y[1])).collect()
    } else {
        let base: Vec<_> = topology
            .rows
            .iter()
            .filter(|r| overlaps((r.top, r.bottom), (mt, mb)))
            .map(|r| (r.top.max(mt), r.bottom.min(mb)))
            .collect();
        let mut bounds: BTreeSet<_> = base.iter().flat_map(|&(a, b)| [a, b]).collect();
        bounds.extend(
            sources
                .iter()
                .filter(|(id, _)| embedded.contains(id))
                .flat_map(|(_, b)| [b[1], b[3]])
                .filter(|&y| mt < y && y < mb),
        );
        let ordered: Vec<_> = bounds.into_iter().collect();
        let mut intervals: Vec<_> = ordered
            .windows(2)
            .filter(|y| base.iter().any(|&(a, b)| a <= y[0] && y[1] <= b))
            .map(|y| (y[0], y[1]))
            .collect();
        let missing: Vec<_> = sources
            .iter()
            .filter(|(_, b)| !intervals.iter().any(|&y| overlaps(y, (b[1], b[3]))))
            .map(|(_, b)| (mt.max(b[1]), mb.min(b[3])))
            .collect();
        let bounds: BTreeSet<_> = missing.iter().flat_map(|&(a, b)| [a, b]).collect();
        let ordered: Vec<_> = bounds.into_iter().collect();
        let extra: Vec<_> = ordered
            .windows(2)
            .filter(|y| {
                y[0] < y[1]
                    && missing.iter().any(|&m| overlaps((y[0], y[1]), m))
                    && !intervals.iter().any(|&i| overlaps((y[0], y[1]), i))
            })
            .map(|y| (y[0], y[1]))
            .collect();
        intervals.extend(extra);
        intervals.sort();
        intervals
    };
    let mut rows = Vec::new();
    let mut values = Vec::new();
    for (top, bottom) in intervals {
        let matching: Vec<_> = topology
            .rows
            .iter()
            .enumerate()
            .filter(|(_, r)| r.top <= top && bottom <= r.bottom)
            .collect();
        let xs: Vec<_> = if let Some(n) = network {
            n.x_lines.clone()
        } else {
            let mut bounds = BTreeSet::from([ml, mr]);
            for (_, row) in &matching {
                for s in &row.slots {
                    if overlaps((s.start, s.end), (ml, mr)) {
                        bounds.insert(ml.max(s.start));
                        bounds.insert(mr.min(s.end));
                    }
                }
            }
            for (id, b) in &sources {
                if (matching.is_empty() || embedded.contains(id))
                    && intersects(*b, [ml, top, mr, bottom])
                {
                    bounds.insert(ml.max(b[0]));
                    bounds.insert(mr.min(b[2]));
                }
            }
            bounds.into_iter().collect()
        };
        let mut cells = Vec::new();
        let mut slots = Vec::new();
        for x in xs.windows(2) {
            let bbox = [x[0], top, x[1], bottom];
            let owners: Vec<_> = sources
                .iter()
                .filter(|(_, b)| intersects(*b, bbox))
                .collect();
            let original: Vec<_> = matching
                .iter()
                .flat_map(|(ri, r)| {
                    r.slots
                        .iter()
                        .enumerate()
                        .filter(|(_, s)| overlaps((s.start, s.end), (x[0], x[1])))
                        .map(move |(ci, s)| (*ri, ci, s))
                })
                .collect();
            let explicitly_empty = network.is_none()
                && !original.is_empty()
                && original
                    .iter()
                    .all(|(_, _, s)| s.value == SpatialValue::Empty)
                && !owners.iter().any(|(id, _)| embedded.contains(id))
                && !owners.iter().any(|(_, b)| {
                    2 * x[0] <= b[0] + b[2]
                        && b[0] + b[2] < 2 * x[1]
                        && 2 * top <= b[1] + b[3]
                        && b[1] + b[3] < 2 * bottom
                });
            let component_ids: BTreeSet<_> = original
                .iter()
                .filter_map(|(ri, ci, _)| components.get(&(*ri, *ci)).copied())
                .collect();
            let source_ids: Vec<_> = if explicitly_empty {
                Vec::new()
            } else {
                owners.iter().map(|(id, _)| *id).collect()
            };
            let value = if owners.is_empty() || explicitly_empty {
                SpatialValue::Empty
            } else if network.is_some() {
                SpatialValue::RuledNetwork(object.reading_index)
            } else if !component_ids.is_empty() {
                SpatialValue::SourceComponents(component_ids.into_iter().collect())
            } else {
                let mut sorted = source_ids.clone();
                sorted.sort_unstable();
                SpatialValue::SourceSegments(sorted)
            };
            slots.push(SpatialSlot {
                start: x[0] - ml,
                end: x[1] - ml,
                value,
            });
            cells.push(LocalCell {
                bbox,
                code: 0,
                segment_indexes: source_ids,
            });
        }
        values.push(slots);
        rows.push(LocalRow { top, bottom, cells });
    }
    let mut group_start = 0;
    for i in 1..=rows.len() {
        if i == rows.len() || rows[i - 1].bottom != rows[i].top {
            let codes = encode_spatial_rows(&values[group_start..i]);
            for (row, codes) in rows[group_start..i].iter_mut().zip(codes) {
                for (cell, code) in row.cells.iter_mut().zip(codes) {
                    cell.code = code;
                }
            }
            group_start = i;
        }
    }
    let represented: BTreeSet<_> = rows
        .iter()
        .flat_map(|r| {
            r.cells
                .iter()
                .flat_map(|c| c.segment_indexes.iter().copied())
        })
        .collect();
    if represented != object.segment_indexes.iter().copied().collect() {
        return None;
    }
    Some(ObjectLocalMatrix { rows })
}

impl ObjectLocalMatrix {
    pub(crate) fn logical_spans(&self) -> Vec<MaterializedSegmentSpan> {
        let mut spans = BTreeMap::<usize, [usize; 4]>::new();
        for (ri, row) in self.rows.iter().enumerate() {
            for (ci, cell) in row.cells.iter().enumerate() {
                for &id in &cell.segment_indexes {
                    spans
                        .entry(id)
                        .and_modify(|s| {
                            s[0] = s[0].min(ri);
                            s[1] = s[1].max(ri + 1);
                            s[2] = s[2].min(ci);
                            s[3] = s[3].max(ci + 1);
                        })
                        .or_insert([ri, ri + 1, ci, ci + 1]);
                }
            }
        }
        spans
            .into_iter()
            .map(|(id, s)| MaterializedSegmentSpan {
                segment_index: id,
                row_start: s[0],
                row_stop: s[1],
                column_start: s[2],
                column_stop: s[3],
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::topology::PhysicalRow;
    fn row(top: usize, bottom: usize, cells: &[(usize, usize, bool, u8)]) -> PhysicalRow {
        PhysicalRow {
            top,
            bottom,
            source_rows: Vec::new(),
            slots: cells
                .iter()
                .map(|&(start, end, payload, _)| SpatialSlot {
                    start,
                    end,
                    value: if payload {
                        SpatialValue::DenseRow(0)
                    } else {
                        SpatialValue::Empty
                    },
                })
                .collect(),
            codes: cells.iter().map(|c| c.3).collect(),
        }
    }
    #[test]
    fn an_upward_cell_does_not_join_independent_upper_payloads() {
        let topology = PhysicalTopology {
            rows: vec![
                row(0, 10, &[(0, 10, true, 0), (10, 20, true, 0)]),
                row(10, 20, &[(0, 20, true, 3)]),
            ],
        };
        let c = payload_components(&topology);
        assert_ne!(c[&(0, 0)], c[&(0, 1)]);
        assert_ne!(c[&(0, 0)], c[&(1, 0)]);
        assert_ne!(c[&(0, 1)], c[&(1, 0)]);
    }
    #[test]
    fn observed_horizontal_identity_allows_a_wide_upward_cell() {
        let topology = PhysicalTopology {
            rows: vec![
                row(0, 10, &[(0, 10, true, 0), (10, 20, true, 5)]),
                row(10, 20, &[(0, 20, true, 3)]),
            ],
        };
        let c = payload_components(&topology);
        assert_eq!(c[&(0, 0)], c[&(0, 1)]);
        assert_eq!(c[&(0, 0)], c[&(1, 0)]);
        let gapped = PhysicalTopology {
            rows: vec![topology.rows[0].clone(), row(11, 20, &[(0, 20, true, 3)])],
        };
        let c = payload_components(&gapped);
        assert_ne!(c[&(0, 0)], c[&(1, 0)]);
    }
    #[test]
    fn geometric_source_ids_survive_repeated_matrix_membership() {
        let matrix = ObjectLocalMatrix {
            rows: vec![
                LocalRow {
                    top: 0,
                    bottom: 10,
                    cells: vec![
                        LocalCell {
                            bbox: [0, 0, 10, 10],
                            code: 0,
                            segment_indexes: vec![42, 7],
                        },
                        LocalCell {
                            bbox: [10, 0, 20, 10],
                            code: 5,
                            segment_indexes: vec![42],
                        },
                    ],
                },
                LocalRow {
                    top: 10,
                    bottom: 20,
                    cells: vec![
                        LocalCell {
                            bbox: [0, 10, 10, 20],
                            code: 3,
                            segment_indexes: vec![42],
                        },
                        LocalCell {
                            bbox: [10, 10, 20, 20],
                            code: 7,
                            segment_indexes: vec![],
                        },
                    ],
                },
            ],
        };
        let spans = matrix.logical_spans();
        assert_eq!(spans.len(), 2);
        let source = spans.iter().find(|s| s.segment_index == 42).unwrap();
        assert_eq!(
            [
                source.row_start,
                source.row_stop,
                source.column_start,
                source.column_stop
            ],
            [0, 2, 0, 2]
        );
        let source = spans.iter().find(|s| s.segment_index == 7).unwrap();
        assert_eq!(
            [
                source.row_start,
                source.row_stop,
                source.column_start,
                source.column_stop
            ],
            [0, 1, 0, 1]
        );
        assert_eq!(matrix.rows[1].cells[1].segment_indexes, Vec::<usize>::new());
    }
}
