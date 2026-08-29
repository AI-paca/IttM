use std::collections::{BTreeMap, BTreeSet};

use crate::geometry::{GeometryAnalysis, MaterializedSegmentSpan};
use crate::objects::{DocumentObject, ObjectKind, ObjectReconstruction};
use crate::topology::{PhysicalTopology, SpatialValue};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RecognitionBlock {
    pub bbox: [usize; 4],
    pub core_segment_indexes: Vec<usize>,
    pub segment_indexes: Vec<usize>,
    pub context_segment_indexes: Vec<usize>,
    pub object_indexes: Vec<usize>,
    pub scope_index: usize,
    pub matrix_window: Option<[usize; 4]>,
    pub dyadic_mask: bool,
    pub matrix_segment_shape: Option<[usize; 2]>,
    pub logical_segment_spans: Vec<[usize; 4]>,
    pub logical_scope_shape: Option<[usize; 2]>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BlockSetAlgebra {
    pub first_block_index: usize,
    pub second_block_index: usize,
    pub intersection_segment_indexes: Vec<usize>,
    pub union_segment_indexes: Vec<usize>,
    pub xor_segment_indexes: Vec<usize>,
    pub first_only_segment_indexes: Vec<usize>,
    pub second_only_segment_indexes: Vec<usize>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MembershipUnit {
    pub segment_indexes: Vec<usize>,
    pub block_indexes: Vec<usize>,
    pub scope_index: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BlockPlan {
    pub source_segment_indexes: Vec<usize>,
    pub blocks: Vec<RecognitionBlock>,
    pub algebra: Vec<BlockSetAlgebra>,
    pub membership_units: Vec<MembershipUnit>,
}

#[derive(Clone, Debug)]
struct Candidate {
    members: Vec<usize>,
    scope_index: usize,
    bbox: Option<[usize; 4]>,
    window: Option<[usize; 4]>,
    dyadic_mask: bool,
    segment_shape: Option<[usize; 2]>,
    logical_spans: Option<Vec<[usize; 4]>>,
    logical_scope_shape: Option<[usize; 2]>,
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

fn box_area(value: [usize; 4]) -> usize {
    (value[2] - value[0]) * (value[3] - value[1])
}

fn clamped_spatial_bbox(
    member_bbox: [usize; 4],
    excluded: impl IntoIterator<Item = [usize; 4]>,
    width: usize,
    height: usize,
) -> Option<[usize; 4]> {
    let mut bbox = [
        member_bbox[0].saturating_sub(8),
        member_bbox[1].saturating_sub(8),
        (member_bbox[2] + 8).min(width),
        (member_bbox[3] + 8).min(height),
    ];
    let excluded: Vec<[usize; 4]> = excluded.into_iter().collect();
    for segment in &excluded {
        if !boxes_intersect(bbox, *segment) {
            continue;
        }
        let mut candidates = Vec::<(usize, usize, [usize; 4])>::new();
        if segment[2] <= member_bbox[0] {
            let candidate = [bbox[0].max(segment[2]), bbox[1], bbox[2], bbox[3]];
            candidates.push((box_area(bbox) - box_area(candidate), 0, candidate));
        }
        if segment[0] >= member_bbox[2] {
            let candidate = [bbox[0], bbox[1], bbox[2].min(segment[0]), bbox[3]];
            candidates.push((box_area(bbox) - box_area(candidate), 1, candidate));
        }
        if segment[3] <= member_bbox[1] {
            let candidate = [bbox[0], bbox[1].max(segment[3]), bbox[2], bbox[3]];
            candidates.push((box_area(bbox) - box_area(candidate), 2, candidate));
        }
        if segment[1] >= member_bbox[3] {
            let candidate = [bbox[0], bbox[1], bbox[2], bbox[3].min(segment[1])];
            candidates.push((box_area(bbox) - box_area(candidate), 3, candidate));
        }
        if candidates.is_empty() {
            if boxes_intersect(member_bbox, *segment) {
                continue;
            }
            return None;
        }
        candidates.sort_by_key(|item| (item.0, item.1));
        bbox = candidates[0].2;
    }
    for segment in &excluded {
        if boxes_intersect(bbox, *segment) && !boxes_intersect(member_bbox, *segment) {
            return None;
        }
    }
    if member_bbox[0].max(bbox[0]) != member_bbox[0]
        || member_bbox[1].max(bbox[1]) != member_bbox[1]
        || member_bbox[2].min(bbox[2]) != member_bbox[2]
        || member_bbox[3].min(bbox[3]) != member_bbox[3]
    {
        return None;
    }
    Some(bbox)
}

fn bit_length(value: usize) -> usize {
    (usize::BITS - value.leading_zeros()) as usize
}

fn dense_shape(member_count: usize) -> Option<[usize; 2]> {
    let square_columns = (member_count as f64).sqrt().ceil() as usize;
    let column_power = bit_length(square_columns.saturating_sub(1));
    let columns = (1_usize << column_power).min(16);
    let required_rows = member_count.div_ceil(columns);
    let rows = 1_usize << bit_length(required_rows.saturating_sub(1));
    (rows <= 16).then_some([rows, columns])
}

fn normalized_fallback_spans(
    segment_indexes: &[usize],
    fallback: &[MaterializedSegmentSpan],
) -> Option<(Vec<MaterializedSegmentSpan>, [usize; 2])> {
    let selected = segment_indexes
        .iter()
        .map(|index| fallback.get(*index).copied())
        .collect::<Option<Vec<_>>>()?;
    let row_start = selected.iter().map(|span| span.row_start).min()?;
    let row_stop = selected.iter().map(|span| span.row_stop).max()?;
    let column_start = selected.iter().map(|span| span.column_start).min()?;
    let column_stop = selected.iter().map(|span| span.column_stop).max()?;
    if row_start >= row_stop || column_start >= column_stop {
        return None;
    }
    let mut spans = fallback.to_vec();
    for (segment_index, source) in segment_indexes.iter().zip(selected) {
        spans[*segment_index] = MaterializedSegmentSpan {
            segment_index: *segment_index,
            row_start: source.row_start - row_start,
            row_stop: source.row_stop - row_start,
            column_start: source.column_start - column_start,
            column_stop: source.column_stop - column_start,
        };
    }
    Some((spans, [row_stop - row_start, column_stop - column_start]))
}

fn object_logical_spans(
    object: &DocumentObject,
    fallback: &[MaterializedSegmentSpan],
) -> Option<(Vec<MaterializedSegmentSpan>, [usize; 2])> {
    let logical = object.logical_spans.as_ref()?;
    if logical.len() != object.segment_indexes.len() {
        return None;
    }
    let members = object
        .segment_indexes
        .iter()
        .copied()
        .collect::<BTreeSet<_>>();
    let mut spans = fallback.to_vec();
    let mut row_stop = 0_usize;
    let mut column_stop = 0_usize;
    for span in logical {
        if !members.contains(&span.segment_index)
            || span.segment_index >= spans.len()
            || span.row_start >= span.row_stop
            || span.column_start >= span.column_stop
        {
            return None;
        }
        spans[span.segment_index] = *span;
        row_stop = row_stop.max(span.row_stop);
        column_stop = column_stop.max(span.column_stop);
    }
    (row_stop > 0 && column_stop > 0).then_some((spans, [row_stop, column_stop]))
}

fn table_local_spans(
    analysis: &GeometryAnalysis,
    topology: &PhysicalTopology,
    segment_indexes: &[usize],
    object_bbox: [usize; 4],
    fallback: &[MaterializedSegmentSpan],
) -> Option<(Vec<MaterializedSegmentSpan>, [usize; 2])> {
    let rows: Vec<_> = topology
        .rows
        .iter()
        .filter(|row| row.top.max(object_bbox[1]) < row.bottom.min(object_bbox[3]))
        .collect();
    if rows.is_empty() {
        return normalized_fallback_spans(segment_indexes, fallback);
    }
    let column_count = rows
        .iter()
        .map(|row| {
            row.slots
                .iter()
                .filter(|slot| slot.start.max(object_bbox[0]) < slot.end.min(object_bbox[2]))
                .count()
        })
        .max()?;
    if column_count == 0 {
        return normalized_fallback_spans(segment_indexes, fallback);
    }
    let mut spans = fallback.to_vec();
    for segment_index in segment_indexes {
        let segment = analysis
            .materialized_segments
            .segments
            .get(*segment_index)?;
        let mut row_start = usize::MAX;
        let mut row_stop = 0_usize;
        let mut column_start = usize::MAX;
        let mut column_stop = 0_usize;
        for (row_index, row) in rows.iter().enumerate() {
            if segment.bbox[1].max(row.top) >= segment.bbox[3].min(row.bottom) {
                continue;
            }
            for (column_index, slot) in row
                .slots
                .iter()
                .filter(|slot| slot.start.max(object_bbox[0]) < slot.end.min(object_bbox[2]))
                .enumerate()
            {
                if slot.value == SpatialValue::Empty
                    || segment.bbox[0].max(slot.start) >= segment.bbox[2].min(slot.end)
                {
                    continue;
                }
                row_start = row_start.min(row_index);
                row_stop = row_stop.max(row_index + 1);
                column_start = column_start.min(column_index);
                column_stop = column_stop.max(column_index + 1);
            }
        }
        if row_start == usize::MAX || column_start == usize::MAX {
            return normalized_fallback_spans(segment_indexes, fallback);
        }
        spans[*segment_index] = MaterializedSegmentSpan {
            segment_index: *segment_index,
            row_start,
            row_stop,
            column_start,
            column_stop,
        };
    }
    Some((spans, [rows.len(), column_count]))
}

fn dyadic_table_masks(
    scope_segments: &[usize],
    spans: &[MaterializedSegmentSpan],
    analysis: &GeometryAnalysis,
) -> Option<(Vec<Vec<usize>>, usize)> {
    let mut units_by_span = BTreeMap::<[usize; 4], Vec<usize>>::new();
    for segment_index in scope_segments {
        let span = spans[*segment_index];
        units_by_span
            .entry([
                span.row_start,
                span.row_stop,
                span.column_start,
                span.column_stop,
            ])
            .or_default()
            .push(*segment_index);
    }
    let ordered_units: Vec<([usize; 4], Vec<usize>)> = units_by_span
        .into_iter()
        .flat_map(|(span, indexes)| indexes.into_iter().map(move |index| (span, vec![index])))
        .collect();
    let unit_count = ordered_units.len();
    if unit_count == 1 && scope_segments.len() == 1 {
        return None;
    }
    let mut masks = Vec::<Vec<usize>>::new();
    let mut seen = BTreeSet::<Vec<usize>>::new();
    let add_units =
        |unit_indexes: Vec<usize>, masks: &mut Vec<Vec<usize>>, seen: &mut BTreeSet<Vec<usize>>| {
            let selected: BTreeSet<usize> = unit_indexes
                .iter()
                .flat_map(|index| ordered_units[*index].1.iter().copied())
                .collect();
            let mut members: Vec<usize> = scope_segments
                .iter()
                .copied()
                .filter(|index| selected.contains(index))
                .collect();
            let nontrivial: Vec<usize> = members
                .iter()
                .copied()
                .filter(|index| {
                    let bbox = analysis.materialized_segments.segments[*index].bbox;
                    (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > 1
                })
                .collect();
            let noise_count = members.len() - nontrivial.len();
            if nontrivial.is_empty() {
                return;
            }
            if noise_count > nontrivial.len() {
                if nontrivial.len() < 2 {
                    return;
                }
                members = nontrivial;
            }
            if members.len() < 2 || !seen.insert(members.clone()) {
                return;
            }
            masks.push(members);
        };

    let mut chunks = Vec::<Vec<usize>>::new();
    let mut pending_indexes = Vec::<usize>::new();
    let mut pending_members = 0_usize;
    for (index, (_, segment_indexes)) in ordered_units.iter().enumerate() {
        let segment_count = segment_indexes.len();
        if segment_count > 256 {
            return None;
        }
        if !pending_indexes.is_empty()
            && (pending_indexes.len() >= 256 || pending_members + segment_count > 256)
        {
            chunks.push(std::mem::take(&mut pending_indexes));
            pending_members = 0;
        }
        pending_indexes.push(index);
        pending_members += segment_count;
    }
    if !pending_indexes.is_empty() {
        chunks.push(pending_indexes);
    }
    if let Some(last) = chunks.last()
        && last
            .iter()
            .map(|index| ordered_units[*index].1.len())
            .sum::<usize>()
            < 2
    {
        if chunks.len() == 1 {
            return None;
        }
        let borrowed = *chunks[chunks.len() - 2].last()?;
        chunks.last_mut()?.push(borrowed);
    }

    for chunk in &chunks {
        let row_coordinates: BTreeSet<usize> = chunk
            .iter()
            .map(|index| ordered_units[*index].0[0])
            .collect();
        let column_coordinates: BTreeSet<usize> = chunk
            .iter()
            .map(|index| ordered_units[*index].0[2])
            .collect();
        let row_position: BTreeMap<usize, usize> = row_coordinates
            .iter()
            .copied()
            .enumerate()
            .map(|(index, coordinate)| (coordinate, index))
            .collect();
        let column_position: BTreeMap<usize, usize> = column_coordinates
            .iter()
            .copied()
            .enumerate()
            .map(|(index, coordinate)| (coordinate, index))
            .collect();
        let dense_position: BTreeMap<usize, [usize; 2]> = chunk
            .iter()
            .map(|index| {
                (
                    *index,
                    [
                        row_position[&ordered_units[*index].0[0]],
                        column_position[&ordered_units[*index].0[2]],
                    ],
                )
            })
            .collect();
        let column_count = column_coordinates.len();
        let mut context_mask_width = 1_usize << (bit_length(column_count) - 1);
        if context_mask_width == column_count && column_count > 1 {
            context_mask_width /= 2;
        }
        add_units(
            chunk
                .iter()
                .copied()
                .filter(|index| dense_position[index][1] < context_mask_width)
                .collect(),
            &mut masks,
            &mut seen,
        );
        add_units(
            chunk
                .iter()
                .copied()
                .filter(|index| dense_position[index][1] >= column_count - context_mask_width)
                .collect(),
            &mut masks,
            &mut seen,
        );
        for (offset, count) in [(0, row_coordinates.len()), (1, column_count)] {
            let axis_bits = bit_length(count.saturating_sub(1)).max(1);
            for bit in 0..axis_bits {
                let mut inside: Vec<usize> = chunk
                    .iter()
                    .copied()
                    .filter(|index| (dense_position[index][offset] >> bit) & 1 != 0)
                    .collect();
                let inside_members = inside
                    .iter()
                    .map(|index| ordered_units[*index].1.len())
                    .sum::<usize>();
                if inside_members < 2 {
                    let inside_set: BTreeSet<usize> = inside.iter().copied().collect();
                    inside = chunk
                        .iter()
                        .copied()
                        .filter(|index| !inside_set.contains(index))
                        .collect();
                }
                add_units(inside, &mut masks, &mut seen);
            }
        }
        let chunk_segments: BTreeSet<usize> = chunk
            .iter()
            .flat_map(|index| ordered_units[*index].1.iter().copied())
            .collect();
        let covered: BTreeSet<usize> = masks
            .iter()
            .flat_map(|members| members.iter().copied())
            .filter(|index| chunk_segments.contains(index))
            .collect();
        if covered != chunk_segments {
            add_units(chunk.clone(), &mut masks, &mut seen);
        }
    }
    (!masks.is_empty()).then_some((masks, unit_count))
}

fn select_candidates(candidates: &[Candidate], source_ids: &[usize]) -> Option<Vec<usize>> {
    let mut selected: BTreeSet<usize> = candidates
        .iter()
        .enumerate()
        .filter_map(|(index, candidate)| candidate.window.is_none().then_some(index))
        .collect();
    let table_scopes: Vec<usize> = candidates
        .iter()
        .filter(|candidate| candidate.window.is_some())
        .map(|candidate| candidate.scope_index)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let source_order: BTreeMap<usize, usize> = source_ids
        .iter()
        .copied()
        .enumerate()
        .map(|(index, segment)| (segment, index))
        .collect();
    for scope_index in table_scopes {
        let candidate_indexes: Vec<usize> = candidates
            .iter()
            .enumerate()
            .filter_map(|(index, candidate)| {
                (candidate.scope_index == scope_index && candidate.window.is_some())
                    .then_some(index)
            })
            .collect();
        let scope_sources: Vec<usize> = source_ids
            .iter()
            .copied()
            .filter(|segment| {
                candidate_indexes
                    .iter()
                    .any(|index| candidates[*index].members.contains(segment))
            })
            .collect();
        let mut grouped = BTreeMap::<Vec<usize>, Vec<usize>>::new();
        for segment in &scope_sources {
            let signature: Vec<usize> = candidate_indexes
                .iter()
                .copied()
                .filter(|index| candidates[*index].members.contains(segment))
                .collect();
            if signature.is_empty() {
                return None;
            }
            grouped.entry(signature).or_default().push(*segment);
        }
        let mut atoms: Vec<Vec<usize>> = grouped.into_values().collect();
        atoms.sort_by_key(|values| {
            values
                .iter()
                .map(|value| source_order[value])
                .min()
                .unwrap_or(usize::MAX)
        });
        for atom in &mut atoms {
            atom.sort_by_key(|value| source_order[value]);
        }
        let mut atom_indexes_by_candidate = BTreeMap::<usize, BTreeSet<usize>>::new();
        for candidate_index in &candidate_indexes {
            let candidate_set: BTreeSet<usize> = candidates[*candidate_index]
                .members
                .iter()
                .copied()
                .collect();
            let mut atom_indexes = BTreeSet::new();
            for (atom_index, atom) in atoms.iter().enumerate() {
                let inside: Vec<bool> = atom
                    .iter()
                    .map(|segment| candidate_set.contains(segment))
                    .collect();
                if inside.iter().any(|value| *value) && !inside.iter().all(|value| *value) {
                    return None;
                }
                if inside.iter().all(|value| *value) {
                    atom_indexes.insert(atom_index);
                }
            }
            atom_indexes_by_candidate.insert(*candidate_index, atom_indexes);
        }
        let mut unresolved = vec![(0..atoms.len()).collect::<Vec<_>>()];
        let mut covered = BTreeSet::<usize>::new();
        let mut membership_counts = vec![0_usize; atoms.len()];
        let mut remaining: BTreeSet<usize> = candidate_indexes.iter().copied().collect();
        let mut scope_selected = Vec::<usize>::new();
        while unresolved.iter().any(|group| group.len() > 1) || covered.len() < atoms.len() {
            let mut best: Option<((usize, usize, usize, isize, isize, isize), usize)> = None;
            for candidate_index in &remaining {
                let candidate_atoms = &atom_indexes_by_candidate[candidate_index];
                let pair_gain = unresolved
                    .iter()
                    .map(|group| {
                        let inside = group
                            .iter()
                            .filter(|atom| candidate_atoms.contains(atom))
                            .count();
                        inside * (group.len() - inside)
                    })
                    .sum::<usize>();
                let coverage_gain = candidate_atoms.difference(&covered).count();
                let gain = pair_gain + coverage_gain;
                if gain == 0 {
                    continue;
                }
                let repeat_burden = candidate_atoms
                    .iter()
                    .map(|atom| membership_counts[*atom])
                    .sum::<usize>();
                let key = (
                    gain,
                    pair_gain,
                    coverage_gain,
                    -(repeat_burden as isize),
                    -(candidate_atoms.len() as isize),
                    -(*candidate_index as isize),
                );
                if best.as_ref().is_none_or(|(previous, _)| key > *previous) {
                    best = Some((key, *candidate_index));
                }
            }
            let candidate_index = best?.1;
            scope_selected.push(candidate_index);
            remaining.remove(&candidate_index);
            let candidate_atoms = &atom_indexes_by_candidate[&candidate_index];
            covered.extend(candidate_atoms);
            for atom in candidate_atoms {
                membership_counts[*atom] += 1;
            }
            let mut refined = Vec::<Vec<usize>>::new();
            for group in unresolved {
                let inside: Vec<usize> = group
                    .iter()
                    .copied()
                    .filter(|atom| candidate_atoms.contains(atom))
                    .collect();
                let outside: Vec<usize> = group
                    .iter()
                    .copied()
                    .filter(|atom| !candidate_atoms.contains(atom))
                    .collect();
                if !inside.is_empty() {
                    refined.push(inside);
                }
                if !outside.is_empty() {
                    refined.push(outside);
                }
            }
            unresolved = refined;
        }
        for candidate_index in scope_selected.clone().into_iter().rev() {
            let reduced: Vec<usize> = scope_selected
                .iter()
                .copied()
                .filter(|index| *index != candidate_index)
                .collect();
            let signatures: Vec<Vec<usize>> = (0..atoms.len())
                .map(|atom| {
                    reduced
                        .iter()
                        .copied()
                        .filter(|index| atom_indexes_by_candidate[index].contains(&atom))
                        .collect()
                })
                .collect();
            let complete = signatures.iter().all(|signature| !signature.is_empty())
                && signatures.iter().collect::<BTreeSet<_>>().len() == signatures.len();
            if complete {
                scope_selected.retain(|index| *index != candidate_index);
            }
        }
        selected.extend(scope_selected);
    }
    Some(selected.into_iter().collect())
}

fn ordered_subset(source_ids: &[usize], values: &BTreeSet<usize>) -> Vec<usize> {
    source_ids
        .iter()
        .copied()
        .filter(|value| values.contains(value))
        .collect()
}

fn spatial_algebra(blocks: &[RecognitionBlock], source_ids: &[usize]) -> Vec<BlockSetAlgebra> {
    let mut memberships = BTreeMap::<usize, Vec<usize>>::new();
    for segment in source_ids {
        memberships.insert(*segment, Vec::new());
    }
    for (block_index, block) in blocks.iter().enumerate() {
        for segment in &block.segment_indexes {
            memberships.entry(*segment).or_default().push(block_index);
        }
    }
    let mut pairs = BTreeSet::<(usize, usize)>::new();
    for indexes in memberships.values() {
        for (offset, first) in indexes.iter().enumerate() {
            for second in &indexes[offset + 1..] {
                if blocks[*first].dyadic_mask
                    && blocks[*second].dyadic_mask
                    && blocks[*first].scope_index == blocks[*second].scope_index
                {
                    pairs.insert((*first, *second));
                }
            }
        }
    }
    pairs
        .into_iter()
        .map(|(first, second)| {
            let first_set: BTreeSet<usize> =
                blocks[first].segment_indexes.iter().copied().collect();
            let second_set: BTreeSet<usize> =
                blocks[second].segment_indexes.iter().copied().collect();
            let intersection = first_set.intersection(&second_set).copied().collect();
            let union = first_set.union(&second_set).copied().collect();
            let xor = first_set
                .symmetric_difference(&second_set)
                .copied()
                .collect();
            let first_only = first_set.difference(&second_set).copied().collect();
            let second_only = second_set.difference(&first_set).copied().collect();
            BlockSetAlgebra {
                first_block_index: first,
                second_block_index: second,
                intersection_segment_indexes: ordered_subset(source_ids, &intersection),
                union_segment_indexes: ordered_subset(source_ids, &union),
                xor_segment_indexes: ordered_subset(source_ids, &xor),
                first_only_segment_indexes: ordered_subset(source_ids, &first_only),
                second_only_segment_indexes: ordered_subset(source_ids, &second_only),
            }
        })
        .collect()
}

fn membership_units(
    blocks: &[RecognitionBlock],
    source_ids: &[usize],
) -> Option<Vec<MembershipUnit>> {
    let mut grouped = BTreeMap::<Vec<usize>, Vec<usize>>::new();
    let mut scope_by_signature = BTreeMap::<Vec<usize>, usize>::new();
    for segment in source_ids {
        let block_indexes: Vec<usize> = blocks
            .iter()
            .enumerate()
            .filter_map(|(index, block)| block.segment_indexes.contains(segment).then_some(index))
            .collect();
        if block_indexes.is_empty() {
            return None;
        }
        let scopes: BTreeSet<usize> = block_indexes
            .iter()
            .map(|index| blocks[*index].scope_index)
            .collect();
        if scopes.len() != 1 {
            return None;
        }
        grouped
            .entry(block_indexes.clone())
            .or_default()
            .push(*segment);
        scope_by_signature.insert(block_indexes, *scopes.first()?);
    }
    let source_order: BTreeMap<usize, usize> = source_ids
        .iter()
        .copied()
        .enumerate()
        .map(|(index, segment)| (segment, index))
        .collect();
    let mut values: Vec<(Vec<usize>, Vec<usize>)> = grouped.into_iter().collect();
    values.sort_by_key(|(_, segments)| {
        segments
            .iter()
            .map(|segment| source_order[segment])
            .min()
            .unwrap_or(usize::MAX)
    });
    Some(
        values
            .into_iter()
            .map(|(block_indexes, segment_indexes)| MembershipUnit {
                scope_index: scope_by_signature[&block_indexes],
                segment_indexes,
                block_indexes,
            })
            .collect(),
    )
}

pub fn plan_blocks(
    analysis: &GeometryAnalysis,
    reconstruction: &ObjectReconstruction,
) -> Option<BlockPlan> {
    let topology = crate::topology::build_physical_topology(analysis)?;
    plan_blocks_with_topology(analysis, reconstruction, &topology)
}

pub fn plan_blocks_with_topology(
    analysis: &GeometryAnalysis,
    reconstruction: &ObjectReconstruction,
    topology: &PhysicalTopology,
) -> Option<BlockPlan> {
    let segments = &analysis.materialized_segments.segments;
    let mut spans = vec![None; segments.len()];
    for span in &analysis.sparse_matrix.spans {
        if span.segment_index >= spans.len() || spans[span.segment_index].is_some() {
            return None;
        }
        spans[span.segment_index] = Some(*span);
    }
    let spans: Vec<MaterializedSegmentSpan> = spans.into_iter().collect::<Option<_>>()?;
    let source_ids = reconstruction.source_segment_indexes.clone();
    let mut candidates = Vec::<Candidate>::new();
    let mut all_table_units_at_most_16 = true;
    let mut has_table = false;
    for (scope_index, object) in reconstruction.objects.iter().enumerate() {
        if object.kind != ObjectKind::Table {
            candidates.push(Candidate {
                members: object.segment_indexes.clone(),
                scope_index,
                bbox: None,
                window: None,
                dyadic_mask: false,
                segment_shape: None,
                logical_spans: None,
                logical_scope_shape: None,
            });
            continue;
        }
        has_table = true;
        let Some((table_spans, logical_scope_shape)) = object_logical_spans(object, &spans)
            .or_else(|| {
                table_local_spans(
                    analysis,
                    topology,
                    &object.segment_indexes,
                    object.bbox,
                    &spans,
                )
            })
        else {
            return None;
        };
        let Some((masks, unit_count)) =
            dyadic_table_masks(&object.segment_indexes, &table_spans, analysis)
        else {
            let members = object.segment_indexes.clone();
            let logical_spans = members
                .iter()
                .map(|index| {
                    let span = table_spans[*index];
                    [
                        span.row_start,
                        span.row_stop,
                        span.column_start,
                        span.column_stop,
                    ]
                })
                .collect();
            candidates.push(Candidate {
                segment_shape: dense_shape(members.len()),
                members,
                scope_index,
                bbox: Some(object.bbox),
                window: Some([0, logical_scope_shape[0], 0, logical_scope_shape[1]]),
                dyadic_mask: false,
                logical_spans: Some(logical_spans),
                logical_scope_shape: Some(logical_scope_shape),
            });
            continue;
        };
        all_table_units_at_most_16 &= unit_count <= 16;
        for members in masks {
            let bbox = bbox_union(members.iter().map(|index| segments[*index].bbox))?;
            let window = [
                members
                    .iter()
                    .map(|index| table_spans[*index].row_start)
                    .min()?,
                members
                    .iter()
                    .map(|index| table_spans[*index].row_stop)
                    .max()?,
                members
                    .iter()
                    .map(|index| table_spans[*index].column_start)
                    .min()?,
                members
                    .iter()
                    .map(|index| table_spans[*index].column_stop)
                    .max()?,
            ];
            let shape = dense_shape(members.len())?;
            let logical_spans = members
                .iter()
                .map(|index| {
                    let span = table_spans[*index];
                    [
                        span.row_start,
                        span.row_stop,
                        span.column_start,
                        span.column_stop,
                    ]
                })
                .collect();
            candidates.push(Candidate {
                members,
                scope_index,
                bbox: Some(bbox),
                window: Some(window),
                dyadic_mask: true,
                segment_shape: Some(shape),
                logical_spans: Some(logical_spans),
                logical_scope_shape: Some(logical_scope_shape),
            });
        }
    }
    let selected = if !has_table || all_table_units_at_most_16 {
        select_candidates(&candidates, &source_ids)?
    } else {
        (0..candidates.len()).collect()
    };
    let candidates: Vec<Candidate> = selected
        .into_iter()
        .map(|index| candidates[index].clone())
        .collect();
    let mut primary_indexes = BTreeMap::<usize, Vec<usize>>::new();
    for segment in &source_ids {
        primary_indexes.insert(*segment, Vec::new());
    }
    for (candidate_index, candidate) in candidates.iter().enumerate() {
        for segment in &candidate.members {
            primary_indexes
                .entry(*segment)
                .or_default()
                .push(candidate_index);
        }
    }
    let carrier: BTreeMap<usize, usize> = source_ids
        .iter()
        .map(|segment| Some((*segment, *primary_indexes.get(segment)?.last()?)))
        .collect::<Option<_>>()?;
    let owner_by_segment: Vec<usize> = {
        let mut owners = vec![usize::MAX; segments.len()];
        for (object_index, object) in reconstruction.objects.iter().enumerate() {
            for segment in &object.segment_indexes {
                owners[*segment] = object_index;
            }
        }
        owners
    };
    let mut blocks = Vec::<RecognitionBlock>::new();
    for (candidate_index, candidate) in candidates.into_iter().enumerate() {
        let core: Vec<usize> = source_ids
            .iter()
            .copied()
            .filter(|segment| carrier.get(segment) == Some(&candidate_index))
            .collect();
        let member_set: BTreeSet<usize> = candidate.members.iter().copied().collect();
        let bbox = if let Some(explicit) = candidate.bbox {
            bbox_union([
                explicit,
                bbox_union(candidate.members.iter().map(|index| segments[*index].bbox))?,
            ])?
        } else {
            let member_bbox =
                bbox_union(candidate.members.iter().map(|index| segments[*index].bbox))?;
            clamped_spatial_bbox(
                member_bbox,
                source_ids
                    .iter()
                    .copied()
                    .filter(|index| !member_set.contains(index))
                    .map(|index| segments[index].bbox),
                analysis.foreground.width,
                analysis.foreground.height,
            )?
        };
        let core_set: BTreeSet<usize> = core.iter().copied().collect();
        let context = candidate
            .members
            .iter()
            .copied()
            .filter(|segment| !core_set.contains(segment))
            .collect();
        let logical_segment_spans = candidate.logical_spans.unwrap_or_else(|| {
            candidate
                .members
                .iter()
                .map(|index| {
                    let span = spans[*index];
                    [
                        span.row_start,
                        span.row_stop,
                        span.column_start,
                        span.column_stop,
                    ]
                })
                .collect()
        });
        let mut object_indexes = Vec::new();
        for segment in &core {
            let object_index = owner_by_segment[*segment];
            if !object_indexes.contains(&object_index) {
                object_indexes.push(object_index);
            }
        }
        blocks.push(RecognitionBlock {
            bbox,
            core_segment_indexes: core,
            segment_indexes: candidate.members,
            context_segment_indexes: context,
            object_indexes,
            scope_index: candidate.scope_index,
            matrix_window: candidate.window,
            dyadic_mask: candidate.dyadic_mask,
            matrix_segment_shape: candidate.segment_shape,
            logical_segment_spans,
            logical_scope_shape: candidate.logical_scope_shape,
        });
    }
    let algebra = spatial_algebra(&blocks, &source_ids);
    let membership_units = membership_units(&blocks, &source_ids)?;
    Some(BlockPlan {
        source_segment_indexes: source_ids,
        blocks,
        algebra,
        membership_units,
    })
}
