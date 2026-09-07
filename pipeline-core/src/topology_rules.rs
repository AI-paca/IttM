//! Finite rule-network topology, ported from annotate_pixel_partition_topology.py.
//! Works on the geometry stage's literal mask and boxes, never OCR text.
use super::{PhysicalRow, SpatialSlot, SpatialValue, VisualRow, overlaps};
use crate::geometry::{GeometryAnalysis, MaterializedRule};
use crate::objects::SpatialBoxIndex;
use std::collections::{BTreeMap, BTreeSet};
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct RuledNetwork {
    pub bbox: [usize; 4],
    pub x_lines: Vec<usize>,
    pub y_lines: Vec<usize>,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
enum Edge {
    Bottom,
    Left,
    Right,
    Top,
}
#[derive(Clone)]
struct EdgeCell {
    edge: Edge,
    coordinate: usize,
    start: usize,
    stop: usize,
    indexes: [usize; 3],
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
fn length(r: &MaterializedRule) -> usize {
    let b = r.summary.bbox;
    if r.summary.horizontal {
        b[2] - b[0]
    } else {
        b[3] - b[1]
    }
}
fn thickness(r: &MaterializedRule) -> usize {
    let b = r.summary.bbox;
    if r.summary.horizontal {
        b[3] - b[1]
    } else {
        b[2] - b[0]
    }
}
fn edge_families(
    rules: &[MaterializedRule],
    width: usize,
    height: usize,
) -> Vec<(Edge, BTreeSet<usize>)> {
    let maximum_thickness = 16.max((width.min(height) as f64 * 0.03).round_ties_even() as usize);
    let mut cells = Vec::new();
    for edge in [Edge::Top, Edge::Bottom, Edge::Left, Edge::Right] {
        let horizontal = matches!(edge, Edge::Top | Edge::Bottom);
        let rails: Vec<_> = rules
            .iter()
            .enumerate()
            .filter(|(_, r)| {
                r.summary.horizontal != horizontal && thickness(r) <= maximum_thickness
            })
            .collect();
        for (index, cross) in rules.iter().enumerate().filter(|(_, r)| {
            r.summary.horizontal == horizontal
                && length(r) >= 32
                && thickness(r) <= maximum_thickness
        }) {
            let b = cross.summary.bbox;
            let (coordinate, start, stop) = if horizontal {
                ((b[1] + b[3]) / 2, b[0], b[2])
            } else {
                ((b[0] + b[2]) / 2, b[1], b[3])
            };
            let minimum = (2 * maximum_thickness)
                .max(((stop - start) as f64 * 0.25).round_ties_even() as usize);
            let eligible: Vec<_> = rails
                .iter()
                .copied()
                .filter(|(_, r)| {
                    let b = r.summary.bbox;
                    length(r) >= minimum
                        && match edge {
                            Edge::Bottom => {
                                b[3] >= height.saturating_sub(8) && b[1].abs_diff(coordinate) <= 16
                            }
                            Edge::Top => b[1] <= 8 && b[3].abs_diff(coordinate) <= 16,
                            Edge::Right => {
                                b[2] >= width.saturating_sub(8) && b[0].abs_diff(coordinate) <= 16
                            }
                            Edge::Left => b[0] <= 8 && b[2].abs_diff(coordinate) <= 16,
                        }
                })
                .collect();
            let nearest = |point: usize| {
                eligible
                    .iter()
                    .filter_map(|(i, r)| {
                        let b = r.summary.bbox;
                        let (a, z) = if horizontal {
                            (b[0], b[2])
                        } else {
                            (b[1], b[3])
                        };
                        let gap = a.saturating_sub(point).max(point.saturating_sub(z));
                        (gap <= 16).then_some((gap, std::cmp::Reverse(length(r)), *i))
                    })
                    .min()
                    .map(|x| x.2)
            };
            if let (Some(first), Some(second)) = (nearest(start), nearest(stop)) {
                if first != second {
                    cells.push(EdgeCell {
                        edge,
                        coordinate,
                        start,
                        stop,
                        indexes: [index, first, second],
                    });
                }
            }
        }
    }
    cells.sort_by_key(|c| (c.edge, c.coordinate, c.start, c.stop));
    let mut distinct: Vec<EdgeCell> = Vec::new();
    for c in cells {
        if !distinct.iter().any(|a| {
            a.edge == c.edge
                && a.coordinate.abs_diff(c.coordinate) <= 16
                && ((a.stop.min(c.stop) as i64 - a.start.max(c.start) as i64) as f64)
                    >= 0.75 * (a.stop - a.start).min(c.stop - c.start) as f64
        }) {
            distinct.push(c);
        }
    }
    let mut parents: Vec<_> = (0..distinct.len()).collect();
    for (i, a) in distinct.iter().enumerate() {
        for (j, b) in distinct.iter().enumerate().skip(i + 1) {
            if a.edge == b.edge
                && a.coordinate.abs_diff(b.coordinate) <= 16
                && a.start
                    .saturating_sub(b.stop)
                    .max(b.start.saturating_sub(a.stop))
                    <= 24
            {
                union(&mut parents, i, j);
            }
        }
    }
    let mut groups: BTreeMap<usize, Vec<&EdgeCell>> = BTreeMap::new();
    for (i, c) in distinct.iter().enumerate() {
        groups.entry(find(&mut parents, i)).or_default().push(c);
    }
    groups
        .into_values()
        .filter(|g| g.len() >= 3)
        .map(|g| (g[0].edge, g.iter().flat_map(|c| c.indexes).collect()))
        .collect()
}
fn dense_lines(
    counts: &[usize],
    length: usize,
    numerator: usize,
    denominator: usize,
    offset: usize,
) -> Vec<usize> {
    let mut runs: Vec<(usize, usize)> = Vec::new();
    for (i, n) in counts.iter().enumerate() {
        if n * denominator < length * numerator {
            continue;
        }
        if let Some(last) = runs.last_mut() {
            if i - last.1 <= 8 {
                last.1 = i + 1;
                continue;
            }
        }
        runs.push((i, i + 1));
    }
    runs.into_iter()
        .map(|(a, b)| offset + (a + b - 1) / 2)
        .collect()
}
pub(crate) fn rule_networks(a: &GeometryAnalysis) -> Option<Vec<RuledNetwork>> {
    let width = a.foreground.width;
    let height = a.foreground.height;
    if a.rule_mask.len() != width.checked_mul(height)? {
        return None;
    }
    let rules = &a.rules;
    let families = edge_families(rules, width, height);
    let mut parents: Vec<_> = (0..rules.len()).collect();
    for (_, ids) in &families {
        let mut it = ids.iter();
        if let Some(first) = it.next() {
            for id in it {
                union(&mut parents, *first, *id);
            }
        }
    }
    let index = SpatialBoxIndex::new(rules.iter().map(|r| r.summary.bbox));
    for (i, r) in rules.iter().enumerate() {
        let b = r.summary.bbox;
        for j in index.intersecting([
            b[0].saturating_sub(3),
            b[1].saturating_sub(3),
            b[2] + 3,
            b[3] + 3,
        ]) {
            if j > i {
                union(&mut parents, i, j);
            }
        }
    }
    let mut groups: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
    for i in 0..rules.len() {
        groups.entry(find(&mut parents, i)).or_default().push(i);
    }
    // Python preserves first-source order, independent of the union representative.
    let mut groups: Vec<_> = groups.into_values().collect();
    groups.sort_by_key(|g| g[0]);
    let mut result = Vec::new();
    for ids in groups {
        let edges: BTreeSet<_> = families
            .iter()
            .filter(|(_, f)| ids.iter().any(|i| f.contains(i)))
            .map(|(e, _)| *e)
            .collect();
        let h = ids.iter().filter(|i| rules[**i].summary.horizontal).count();
        let v = ids.len() - h;
        if !(h >= 2 && v >= 2)
            && !edges.iter().any(|e| match e {
                Edge::Top | Edge::Bottom => h >= 1 && v >= 2,
                _ => v >= 1 && h >= 2,
            })
        {
            continue;
        }
        let mut b = [width, height, 0, 0];
        for i in ids {
            let x = rules[i].summary.bbox;
            b = [
                b[0].min(x[0]),
                b[1].min(x[1]),
                b[2].max(x[2]),
                b[3].max(x[3]),
            ];
        }
        b[2] = b[2].min(width);
        b[3] = b[3].min(height);
        if edges.contains(&Edge::Left) {
            b[0] = 0;
        }
        if edges.contains(&Edge::Top) {
            b[1] = 0;
        }
        if edges.contains(&Edge::Right) {
            b[2] = width;
        }
        if edges.contains(&Edge::Bottom) {
            b[3] = height;
        }
        if b[0] >= b[2] || b[1] >= b[3] {
            continue;
        }
        let mut rows = vec![0; b[3] - b[1]];
        let mut cols = vec![0; b[2] - b[0]];
        for y in b[1]..b[3] {
            for x in b[0]..b[2] {
                if a.rule_mask[y * width + x] != 0 {
                    rows[y - b[1]] += 1;
                    cols[x - b[0]] += 1;
                }
            }
        }
        let xs = dense_lines(&cols, b[3] - b[1], 1, 4, b[0]);
        let ys = dense_lines(&rows, b[2] - b[0], 1, 2, b[1]);
        if xs.is_empty() || ys.is_empty() {
            continue;
        }
        let mut xs: BTreeSet<_> = xs.into_iter().collect();
        let mut ys: BTreeSet<_> = ys.into_iter().collect();
        if b[0] == 0 {
            xs.insert(0);
        }
        if b[2] == width {
            xs.insert(width);
        }
        if b[1] == 0 {
            ys.insert(0);
        }
        if b[3] == height {
            ys.insert(height);
        }
        if xs.len() >= 2 && ys.len() >= 2 {
            result.push(RuledNetwork {
                bbox: b,
                x_lines: xs.into_iter().collect(),
                y_lines: ys.into_iter().collect(),
            });
        }
    }
    result.sort_by_key(|r| (r.bbox[1], r.bbox[0]));
    Some(result)
}
fn active_lines(n: &RuledNetwork, a: &GeometryAnalysis, top: usize, bottom: usize) -> Vec<usize> {
    let margin = 2.max(8.min((bottom - top) / 8));
    let start = bottom.min(top + margin);
    let stop = (start + 1)
        .max(bottom.saturating_sub(margin))
        .min(a.foreground.height);
    let width = a.foreground.width;
    n.x_lines
        .iter()
        .enumerate()
        .filter(|(i, x)| {
            *i == 0
                || *i + 1 == n.x_lines.len()
                || (x.saturating_sub(8)..(**x + 9).min(width)).any(|xx| {
                    let count = (start..stop)
                        .filter(|y| a.rule_mask[y * width + xx] != 0)
                        .count();
                    count * 4 >= stop.saturating_sub(start) && stop > start
                })
        })
        .map(|(_, x)| *x)
        .collect()
}
fn subtract(b: [usize; 4], cut: [usize; 4]) -> Vec<[usize; 4]> {
    let c = [
        b[0].max(cut[0]),
        b[1].max(cut[1]),
        b[2].min(cut[2]),
        b[3].min(cut[3]),
    ];
    if c[0] >= c[2] || c[1] >= c[3] {
        return vec![b];
    }
    [
        [b[0], b[1], b[2], c[1]],
        [b[0], c[3], b[2], b[3]],
        [b[0], c[1], c[0], c[3]],
        [c[2], c[1], b[2], c[3]],
    ]
    .into_iter()
    .filter(|p| p[0] < p[2] && p[1] < p[3])
    .collect()
}
fn outer(n: &RuledNetwork) -> [usize; 4] {
    [
        n.x_lines[0],
        n.y_lines[0],
        *n.x_lines.last().unwrap(),
        *n.y_lines.last().unwrap(),
    ]
}
pub(super) fn network_rows(
    a: &GeometryAnalysis,
    networks: &[RuledNetwork],
    visual: &[VisualRow],
) -> Option<Vec<PhysicalRow>> {
    if networks.is_empty() {
        return Some(Vec::new());
    }
    let width = a.foreground.width;
    let single = networks.len() == 1;
    let mut outside = Vec::new();
    for row in visual {
        let mut boxes = row.boxes.clone();
        if single {
            let n = outer(&networks[0]);
            let mut lefts = Vec::new();
            let mut rights = Vec::new();
            for b in boxes {
                let t = b[1].max(n[1]);
                let z = b[3].min(n[3]);
                if t >= z {
                    continue;
                }
                if b[0] < n[0] {
                    lefts.push([b[0], t, b[2].min(n[0]), z]);
                }
                if b[2] > n[2] {
                    rights.push([b[0].max(n[2]), t, b[2], z]);
                }
            }
            boxes = Vec::new();
            for side in [lefts, rights] {
                let side: Vec<_> = side.into_iter().filter(|b| b[0] < b[2]).collect();
                if !side.is_empty() {
                    boxes.push([
                        side.iter().map(|b| b[0]).min()?,
                        side.iter().map(|b| b[1]).min()?,
                        side.iter().map(|b| b[2]).max()?,
                        side.iter().map(|b| b[3]).max()?,
                    ]);
                }
            }
        } else {
            for n in networks {
                boxes = boxes
                    .into_iter()
                    .flat_map(|b| subtract(b, outer(n)))
                    .collect();
            }
        }
        if !boxes.is_empty() {
            outside.push(VisualRow {
                row_index: row.row_index,
                boxes,
            });
        }
    }
    let mut boundaries: BTreeSet<usize> = networks
        .iter()
        .flat_map(|n| n.y_lines.iter().copied())
        .collect();
    let top = *boundaries.first()?;
    let bottom = *boundaries.last()?;
    for row in &outside {
        for b in &row.boxes {
            if overlaps((top, bottom), (b[1], b[3])) {
                boundaries.extend([top.max(b[1]), bottom.min(b[3])]);
            }
        }
    }
    let matrix = &a.sparse_matrix;
    let hr: BTreeSet<_> = matrix.horizontal_rule_rows.iter().copied().collect();
    let vc: BTreeSet<_> = matrix.vertical_rule_columns.iter().copied().collect();
    let occupied: BTreeSet<_> = matrix
        .cells
        .iter()
        .filter(|c| !hr.contains(&c.row) && !vc.contains(&c.column))
        .map(|c| {
            [
                matrix.columns[c.column].start,
                matrix.rows[c.row].start,
                matrix.columns[c.column].end,
                matrix.rows[c.row].end,
            ]
        })
        .collect();
    let occupied_index = SpatialBoxIndex::new(occupied.into_iter());
    // Cache unsplit cells once per network band, preserving payload across subdivisions.
    let bands: Vec<Vec<(usize, usize, Vec<SpatialSlot>)>> = networks
        .iter()
        .enumerate()
        .map(|(i, n)| {
            n.y_lines
                .windows(2)
                .map(|b| {
                    let lines = active_lines(n, a, b[0], b[1]);
                    let slots = lines
                        .windows(2)
                        .map(|x| SpatialSlot {
                            start: x[0],
                            end: x[1],
                            value: if occupied_index
                                .intersecting([x[0], b[0] + 1, x[1], (b[0] + 2).max(b[1])])
                                .is_empty()
                            {
                                SpatialValue::Empty
                            } else {
                                SpatialValue::RuledNetwork(i)
                            },
                        })
                        .collect();
                    (b[0], b[1], slots)
                })
                .collect()
        })
        .collect();
    let ys: Vec<_> = boundaries.into_iter().collect();
    let mut result = Vec::new();
    for y in ys.windows(2) {
        let (top, bottom) = (y[0], y[1]);
        let active: Vec<_> = networks
            .iter()
            .enumerate()
            .filter(|(_, n)| n.y_lines[0] <= top && bottom <= *n.y_lines.last().unwrap())
            .collect();
        if active.is_empty() {
            continue;
        }
        let mut blocked: Vec<_> = active
            .iter()
            .map(|(_, n)| (n.x_lines[0], *n.x_lines.last().unwrap()))
            .collect();
        blocked.sort();
        if blocked.windows(2).any(|b| b[0].1 > b[1].0) {
            return None;
        }
        let mut free = Vec::new();
        let mut cursor = 0;
        for (l, r) in blocked {
            if cursor < l {
                free.push((cursor, l));
            }
            cursor = cursor.max(r);
        }
        if cursor < width {
            free.push((cursor, width));
        }
        let mut flow = Vec::new();
        for row in &outside {
            for (l, r) in &free {
                let fragments: Vec<_> = row
                    .boxes
                    .iter()
                    .filter(|b| {
                        overlaps((top, bottom), (b[1], b[3])) && b[0].max(*l) < b[2].min(*r)
                    })
                    .collect();
                if !fragments.is_empty() {
                    flow.push((
                        fragments.iter().map(|b| b[0].max(*l)).min()?,
                        fragments.iter().map(|b| b[2].min(*r)).max()?,
                        row.row_index,
                    ));
                }
            }
        }
        let table: Vec<_> = active
            .iter()
            .flat_map(|(i, _)| {
                bands[*i]
                    .iter()
                    .find(|b| b.0 <= top && bottom <= b.1)
                    .into_iter()
                    .flat_map(|b| b.2.iter())
            })
            .collect();
        let mut xs = BTreeSet::from([0, width]);
        for s in &table {
            xs.extend([s.start, s.end]);
        }
        for (l, r, _) in &flow {
            xs.extend([*l, *r]);
        }
        let xs: Vec<_> = xs.into_iter().collect();
        let slots = xs
            .windows(2)
            .map(|x| {
                let value = if let Some(s) = table.iter().find(|s| s.start <= x[0] && x[1] <= s.end)
                {
                    s.value.clone()
                } else {
                    let ids: Vec<_> = flow
                        .iter()
                        .filter(|(l, r, _)| overlaps((x[0], x[1]), (*l, *r)))
                        .map(|(_, _, i)| *i)
                        .collect();
                    if ids.is_empty() {
                        SpatialValue::Empty
                    } else {
                        SpatialValue::VisualRows(ids)
                    }
                };
                SpatialSlot {
                    start: x[0],
                    end: x[1],
                    value,
                }
            })
            .collect();
        result.push(PhysicalRow {
            source_rows: matrix
                .rows
                .iter()
                .enumerate()
                .filter(|(_, r)| overlaps((top, bottom), (r.start, r.end)))
                .map(|(i, _)| i)
                .collect(),
            top,
            bottom,
            slots,
            codes: Vec::new(),
        });
    }
    Some(result)
}
