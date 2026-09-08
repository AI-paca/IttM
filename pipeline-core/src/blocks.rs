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

const MAX_LOCAL_BLOCK_MEMBERS: usize = 256;
const MAX_BALANCED_SIGNATURE_BLOCKS: usize = 60;

fn packed_shape(member_count: usize) -> Option<[usize; 2]> {
    if !(2..=MAX_LOCAL_BLOCK_MEMBERS).contains(&member_count) {
        return None;
    }
    let rows = member_count.div_ceil(16).min(16);
    let columns = member_count.div_ceil(rows).min(16);
    (rows * columns >= member_count).then_some([rows, columns])
}

fn signature_family_is_identifying(family: &[Vec<usize>], source_count: usize) -> bool {
    if source_count == 0 {
        return false;
    }
    let mut signatures = vec![Vec::<usize>::new(); source_count];
    for (block_index, members) in family.iter().enumerate() {
        for member in members {
            if *member >= source_count {
                return false;
            }
            signatures[*member].push(block_index);
        }
    }
    let mut seen = BTreeSet::<Vec<usize>>::new();
    signatures
        .into_iter()
        .all(|signature| !signature.is_empty() && seen.insert(signature))
}

fn combinations(bit_count: usize, weight: usize) -> Vec<Vec<usize>> {
    fn append(
        start: usize,
        remaining: usize,
        bit_count: usize,
        current: &mut Vec<usize>,
        output: &mut Vec<Vec<usize>>,
    ) {
        if remaining == 0 {
            output.push(current.clone());
            return;
        }
        let stop = bit_count - remaining;
        for bit in start..=stop {
            current.push(bit);
            append(bit + 1, remaining - 1, bit_count, current, output);
            current.pop();
        }
    }

    let mut output = Vec::new();
    if weight <= bit_count {
        append(0, weight, bit_count, &mut Vec::new(), &mut output);
    }
    output
}

fn combination_count(bit_count: usize, weight: usize) -> usize {
    let weight = weight.min(bit_count.saturating_sub(weight));
    let mut value = 1_u128;
    for offset in 0..weight {
        value = value * (bit_count - offset) as u128 / (offset + 1) as u128;
    }
    value.min(usize::MAX as u128) as usize
}

fn balanced_sparse_signature_family(source_count: usize) -> Option<Vec<Vec<usize>>> {
    if source_count < 3 || source_count > MAX_LOCAL_BLOCK_MEMBERS {
        return None;
    }
    let first_bit_count = bit_length(source_count).max(2);
    for bit_count in first_bit_count..=MAX_BALANCED_SIGNATURE_BLOCKS {
        for minimum_weight in [1_usize, 2] {
            let mut remaining = source_count;
            let mut minimum_memberships = 0_usize;
            for weight in minimum_weight..=bit_count {
                let take = remaining.min(combination_count(bit_count, weight));
                minimum_memberships = minimum_memberships.saturating_add(take * weight);
                remaining -= take;
                if remaining == 0 {
                    break;
                }
            }
            if remaining != 0 || minimum_memberships > bit_count * MAX_LOCAL_BLOCK_MEMBERS {
                continue;
            }

            let mut codes = Vec::<Vec<usize>>::new();
            let mut loads = vec![0_usize; bit_count];
            remaining = source_count;
            let mut feasible = true;
            for weight in minimum_weight..=bit_count {
                if remaining == 0 {
                    break;
                }
                let mut available = combinations(bit_count, weight);
                if remaining >= available.len() {
                    if (0..bit_count).any(|bit| {
                        loads[bit] + available.iter().filter(|code| code.contains(&bit)).count()
                            > MAX_LOCAL_BLOCK_MEMBERS
                    }) {
                        feasible = false;
                        break;
                    }
                    remaining -= available.len();
                    for code in &available {
                        for bit in code {
                            loads[*bit] += 1;
                        }
                    }
                    codes.append(&mut available);
                    continue;
                }

                for _selection in 0..remaining {
                    let best = available
                        .iter()
                        .enumerate()
                        .filter(|(_, code)| {
                            code.iter().all(|bit| loads[*bit] < MAX_LOCAL_BLOCK_MEMBERS)
                        })
                        .min_by_key(|(_, code)| {
                            let mut ordered_loads: Vec<usize> =
                                code.iter().map(|bit| loads[*bit]).collect();
                            ordered_loads.sort_unstable();
                            (
                                code.iter().map(|bit| loads[*bit] + 1).max().unwrap_or(0),
                                code.iter().map(|bit| loads[*bit]).sum::<usize>(),
                                ordered_loads,
                                (*code).clone(),
                            )
                        })
                        .map(|(index, _)| index);
                    let Some(best) = best else {
                        feasible = false;
                        break;
                    };
                    let code = available.remove(best);
                    for bit in &code {
                        loads[*bit] += 1;
                    }
                    codes.push(code);
                }
                remaining = 0;
                break;
            }
            if !feasible || remaining != 0 || codes.len() != source_count {
                continue;
            }
            let family: Vec<Vec<usize>> = (0..bit_count)
                .map(|bit| {
                    codes
                        .iter()
                        .enumerate()
                        .filter_map(|(source, code)| code.contains(&bit).then_some(source))
                        .collect()
                })
                .collect();
            if family
                .iter()
                .all(|members| (2..=MAX_LOCAL_BLOCK_MEMBERS).contains(&members.len()))
                && signature_family_is_identifying(&family, source_count)
            {
                return Some(family);
            }
        }
    }
    None
}

fn locality_preserving_regions(source_count: usize) -> Option<Vec<Vec<usize>>> {
    if source_count < 3 {
        return None;
    }
    let region_count = source_count.div_ceil(MAX_LOCAL_BLOCK_MEMBERS);
    let base_size = source_count / region_count;
    let larger_regions = source_count % region_count;
    let mut offset = 0_usize;
    let mut regions = Vec::with_capacity(region_count);
    for region_index in 0..region_count {
        let size = base_size + usize::from(region_index < larger_regions);
        if !(2..=MAX_LOCAL_BLOCK_MEMBERS).contains(&size) {
            return None;
        }
        regions.push((offset..offset + size).collect());
        offset += size;
    }
    (offset == source_count).then_some(regions)
}

fn locality_region_codes(regions: &[Vec<usize>]) -> Option<Vec<Vec<usize>>> {
    let maximum_size = regions.iter().map(Vec::len).max()?;
    let bit_count = bit_length(maximum_size.saturating_sub(1)).max(1);
    let mut output = Vec::with_capacity(regions.len());
    for region in regions {
        let mut codes: Vec<usize> = (0..region.len()).collect();
        let mut used: BTreeSet<usize> = codes.iter().copied().collect();
        for bit in 0..bit_count {
            let mask = 1_usize << bit;
            if codes.iter().filter(|code| **code & mask != 0).count() != 1 {
                continue;
            }
            let replacement_index = codes
                .iter()
                .position(|code| *code & mask == 0 && !used.contains(&(*code | mask)))?;
            let original = codes[replacement_index];
            let replacement = original | mask;
            used.remove(&original);
            used.insert(replacement);
            codes[replacement_index] = replacement;
        }
        if codes.iter().copied().collect::<BTreeSet<_>>().len() != codes.len() {
            return None;
        }
        output.push(codes);
    }
    Some(output)
}

fn locality_preserving_signature_family(source_count: usize) -> Option<Vec<Vec<usize>>> {
    let regions = locality_preserving_regions(source_count)?;
    let mut family = regions.clone();
    if regions.len() == 1 {
        for members in balanced_sparse_signature_family(source_count)? {
            if !family.contains(&members) {
                family.push(members);
            }
        }
    } else {
        let codes = locality_region_codes(&regions)?;
        let bit_count = bit_length(regions.iter().map(Vec::len).max()?.saturating_sub(1)).max(1);
        for bit in 0..bit_count {
            let mut pending = Vec::<usize>::new();
            for (region, region_codes) in regions.iter().zip(&codes) {
                let island: Vec<usize> = region
                    .iter()
                    .zip(region_codes)
                    .filter_map(|(source, code)| ((code >> bit) & 1 != 0).then_some(*source))
                    .collect();
                if island.is_empty() {
                    continue;
                }
                if !pending.is_empty() && pending.len() + island.len() > MAX_LOCAL_BLOCK_MEMBERS {
                    family.push(std::mem::take(&mut pending));
                }
                pending.extend(island);
            }
            if !pending.is_empty() {
                family.push(pending);
            }
        }
    }
    (family
        .iter()
        .all(|members| (2..=MAX_LOCAL_BLOCK_MEMBERS).contains(&members.len()))
        && signature_family_is_identifying(&family, source_count))
    .then_some(family)
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
    if let Some(matrix) = &object.local_matrix {
        row_stop = matrix.rows.len();
        column_stop = matrix.rows.iter().map(|r| r.cells.len()).max().unwrap_or(0);
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
    // A single 16x16 OCR block can preserve the table's intrinsic row/column
    // algebra.  Only tables that exceed that physical block limit need the
    // bounded locality signature family; applying it to smaller tables
    // discards useful table geometry and turns ordinary cells into a sparse
    // polar atlas.
    if unit_count > MAX_LOCAL_BLOCK_MEMBERS {
        let unit_masks = locality_preserving_signature_family(unit_count)?;
        let masks = unit_masks
            .into_iter()
            .map(|unit_indexes| {
                unit_indexes
                    .into_iter()
                    .flat_map(|index| ordered_units[index].1.iter().copied())
                    .collect::<Vec<_>>()
            })
            .collect::<Vec<_>>();
        return Some((masks, unit_count));
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
    // Residual geometry retains ownership but is excluded from Python OCR plans.
    let active: BTreeSet<_> = reconstruction.objects.iter()
        .filter(|o| o.is_recognizable()).flat_map(|o| o.segment_indexes.iter().copied()).collect();
    let source_ids = reconstruction.source_segment_indexes.iter().copied()
        .filter(|id| active.contains(id)).collect::<Vec<_>>();
    let mut candidates = Vec::<Candidate>::new();
    let mut all_table_units_at_most_16 = true;
    let mut has_table = false;
    for (scope_index, object) in reconstruction.objects.iter().enumerate() {
        if !object.is_recognizable() { continue; }
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
        let locality_packed = unit_count > MAX_LOCAL_BLOCK_MEMBERS;
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
            let shape = if locality_packed {
                packed_shape(members.len())?
            } else {
                dense_shape(members.len())?
            };
            let logical_spans = if locality_packed {
                members
                    .iter()
                    .enumerate()
                    .map(|(position, _index)| {
                        let row = position / shape[1];
                        let column = position % shape[1];
                        [row, row + 1, column, column + 1]
                    })
                    .collect()
            } else {
                members
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
                    .collect()
            };
            candidates.push(Candidate {
                members,
                scope_index,
                bbox: Some(bbox),
                window: Some(window),
                dyadic_mask: true,
                segment_shape: Some(shape),
                logical_spans: Some(logical_spans),
                logical_scope_shape: Some(if locality_packed {
                    shape
                } else {
                    logical_scope_shape
                }),
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

#[cfg(test)]
mod tests {
    use super::{
        MAX_LOCAL_BLOCK_MEMBERS, locality_preserving_signature_family, packed_shape,
        signature_family_is_identifying,
    };

    fn assert_python_locality_parity(source_count: usize, expected_blocks: usize) {
        let family = locality_preserving_signature_family(source_count)
            .expect("Python locality/signature family must be constructible");
        assert_eq!(family.len(), expected_blocks);
        assert!(
            family
                .iter()
                .all(|members| (2..=MAX_LOCAL_BLOCK_MEMBERS).contains(&members.len()))
        );
        assert!(signature_family_is_identifying(&family, source_count));
        let maximum_memberships = (0..source_count)
            .map(|source| {
                family
                    .iter()
                    .filter(|members| members.contains(&source))
                    .count()
            })
            .max()
            .unwrap_or(0);
        assert_eq!(maximum_memberships, 8);
    }

    #[test]
    fn python_locality_family_has_fifty_blocks_for_2427_segments() {
        assert_python_locality_parity(2427, 50);
    }

    #[test]
    fn current_rust_object_has_bounded_family_for_2596_segments() {
        assert_python_locality_parity(2596, 59);
    }

    #[test]
    fn packed_shapes_never_create_more_than_a_sixteen_by_sixteen_slot() {
        assert_eq!(packed_shape(228), Some([15, 16]));
        assert_eq!(packed_shape(243), Some([16, 16]));
        assert_eq!(packed_shape(256), Some([16, 16]));
    }
}
