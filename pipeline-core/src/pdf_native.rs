use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

use crate::assembler::{
    AssemblerError, AssemblerSegment, AssemblerSource, SegmentObjectLayout,
    StructuralObjectKind, TopologyRenderArtifact, assemble_segment_topology,
};

#[derive(Clone, Debug, PartialEq)]
pub struct PdfTextGeometryItem {
    pub text: String,
    pub width: f64,
    pub height: Option<f64>,
    pub transform: [f64; 6],
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PdfTextBox {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct NativeTextCandidate {
    source_item_index: usize,
    bbox: PdfTextBox,
    text: String,
}

#[derive(Clone, Debug, PartialEq)]
struct NativeRow {
    center_y: f64,
    height: i32,
    bbox: PdfTextBox,
    candidate_indexes: Vec<usize>,
}

impl NativeRow {
    fn center_y(&self) -> f64 {
        self.center_y
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeTextCell {
    pub row: u32,
    pub column: u32,
    pub row_span: u32,
    pub column_span: u32,
    pub bbox: PdfTextBox,
    pub candidate_indexes: Vec<usize>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeTextObject {
    pub object_id: String,
    pub kind: StructuralObjectKind,
    pub bbox: PdfTextBox,
    pub rows: Vec<Vec<usize>>,
    pub cells: Vec<NativeTextCell>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativePdfSegment {
    pub segment_id: String,
    pub source_item_index: usize,
    pub bbox: PdfTextBox,
    pub object_id: String,
    pub object_kind: StructuralObjectKind,
    pub row: u32,
    pub column: u32,
    pub row_span: u32,
    pub column_span: u32,
    pub text: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativePdfArtifact {
    pub objects: Vec<NativeTextObject>,
    pub segments: Vec<NativePdfSegment>,
    pub assembled: TopologyRenderArtifact,
}

fn rounded_coordinate(value: f64) -> i32 {
    if !value.is_finite() {
        return 0;
    }
    value.round().clamp(i32::MIN as f64, i32::MAX as f64) as i32
}

fn candidate_from_item(
    item: &PdfTextGeometryItem,
    source_item_index: usize,
) -> Option<NativeTextCandidate> {
    let text = item.text.trim();
    if text.is_empty() {
        return None;
    }
    let x = item.transform[4];
    let baseline = item.transform[5];
    let width = item.width.abs().max(1.0);
    let transform_height = item.transform[2].hypot(item.transform[3]);
    let height = item.height.unwrap_or(transform_height).abs().max(1.0);
    Some(NativeTextCandidate {
        source_item_index,
        bbox: PdfTextBox {
            left: rounded_coordinate(x),
            top: rounded_coordinate(baseline - height),
            right: rounded_coordinate(x + width),
            bottom: rounded_coordinate(baseline),
        },
        text: text.to_owned(),
    })
}

fn union_boxes(boxes: impl IntoIterator<Item = PdfTextBox>) -> Option<PdfTextBox> {
    let mut boxes = boxes.into_iter();
    let mut result = boxes.next()?;
    for bbox in boxes {
        result.left = result.left.min(bbox.left);
        result.top = result.top.min(bbox.top);
        result.right = result.right.max(bbox.right);
        result.bottom = result.bottom.max(bbox.bottom);
    }
    Some(result)
}

fn median(values: impl IntoIterator<Item = i32>) -> f64 {
    let mut values = values.into_iter().collect::<Vec<_>>();
    if values.is_empty() {
        return 0.0;
    }
    values.sort_unstable();
    let middle = values.len() / 2;
    if values.len().is_multiple_of(2) {
        (f64::from(values[middle - 1]) + f64::from(values[middle])) / 2.0
    } else {
        f64::from(values[middle])
    }
}

fn aligned_column_count(
    left: &NativeRow,
    right: &NativeRow,
    candidates: &[NativeTextCandidate],
) -> usize {
    let tolerance = 3.0_f64.max(f64::from(left.height.min(right.height)) * 0.8);
    let mut available = right
        .candidate_indexes
        .iter()
        .copied()
        .collect::<BTreeSet<_>>();
    let mut matches = 0;
    for left_index in &left.candidate_indexes {
        let left_x = candidates[*left_index].bbox.left;
        let nearest = available
            .iter()
            .copied()
            .filter_map(|right_index| {
                let distance = f64::from((candidates[right_index].bbox.left - left_x).abs());
                (distance <= tolerance).then_some((right_index, distance))
            })
            .min_by(|left, right| {
                left.1
                    .partial_cmp(&right.1)
                    .unwrap_or(Ordering::Equal)
                    .then(left.0.cmp(&right.0))
            });
        let Some((nearest, _)) = nearest else {
            continue;
        };
        available.remove(&nearest);
        matches += 1;
    }
    matches
}

fn rows_are_adjacent(left: &NativeRow, right: &NativeRow) -> bool {
    (left.center_y() - right.center_y()).abs()
        <= 18.0_f64.max(f64::from(left.height.max(right.height)) * 3.0)
}

fn row_continues_table(row: &NativeRow, table_box: PdfTextBox, page_width: f64) -> bool {
    let margin = 12.0_f64.max(page_width * 0.03);
    let row_width = f64::from(row.bbox.right - row.bbox.left);
    let table_width = f64::from(table_box.right - table_box.left);
    let overlap = row.bbox.right.min(table_box.right) - row.bbox.left.max(table_box.left);
    let left_aligned = f64::from(row.bbox.left) <= f64::from(table_box.left) + margin
        && f64::from(row.bbox.right) >= f64::from(table_box.left) - margin;
    let broad = overlap > 0
        && row_width >= table_width * 0.45
        && f64::from(overlap) * 2.0 >= row_width;
    left_aligned || broad
}

fn row_aligns_with_established_table_column(
    row: &NativeRow,
    table_rows: &[NativeRow],
    candidates: &[NativeTextCandidate],
) -> bool {
    if row.candidate_indexes.len() != 1 || table_rows.len() < 2 {
        return false;
    }
    let candidate = &candidates[row.candidate_indexes[0]];
    let tolerance = 3.0_f64.max(f64::from(row.height) * 0.8);
    table_rows
        .iter()
        .filter(|table_row| {
            table_row.candidate_indexes.iter().any(|candidate_index| {
                f64::from(
                    (candidates[*candidate_index].bbox.left - candidate.bbox.left).abs(),
                ) <= tolerance
            })
        })
        .count()
        >= 2
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct NativeRowRange {
    start: usize,
    end: usize,
}

fn find_table_ranges(
    rows: &[NativeRow],
    candidates: &[NativeTextCandidate],
) -> Vec<NativeRowRange> {
    if rows.len() < 2 {
        return Vec::new();
    }
    let Some(page_box) = union_boxes(candidates.iter().map(|candidate| candidate.bbox)) else {
        return Vec::new();
    };
    let page_width = f64::from((page_box.right - page_box.left).max(1));
    let table_seeds = rows
        .iter()
        .enumerate()
        .map(|(index, row)| {
            if row.candidate_indexes.len() >= 4 {
                return true;
            }
            if row.candidate_indexes.len() < 2 {
                return false;
            }
            [index.checked_sub(1), (index + 1 < rows.len()).then_some(index + 1)]
                .into_iter()
                .flatten()
                .any(|neighbor| {
                    rows[neighbor].candidate_indexes.len() >= 2
                        && rows_are_adjacent(row, &rows[neighbor])
                        && aligned_column_count(row, &rows[neighbor], candidates) >= 2
                })
        })
        .collect::<Vec<_>>();

    let mut ranges = Vec::new();
    let mut cursor = 0;
    while cursor < rows.len() {
        while cursor < rows.len() && !table_seeds[cursor] {
            cursor += 1;
        }
        if cursor >= rows.len() {
            break;
        }
        let mut start = cursor;
        let mut end = cursor;
        let mut table_box = rows[cursor].bbox;
        let mut seed_count = 0;
        let mut index = cursor;
        while index < rows.len() {
            if index > cursor && !rows_are_adjacent(&rows[index - 1], &rows[index]) {
                break;
            }
            let sandwiched = index > cursor
                && index + 1 < rows.len()
                && table_seeds[index - 1]
                && table_seeds[index + 1]
                && rows_are_adjacent(&rows[index], &rows[index + 1]);
            if !table_seeds[index]
                && !sandwiched
                && !row_continues_table(&rows[index], table_box, page_width)
            {
                break;
            }
            if table_seeds[index] {
                seed_count += 1;
            }
            end = index;
            table_box = union_boxes([table_box, rows[index].bbox]).unwrap_or(table_box);
            index += 1;
        }
        if seed_count < 2 {
            cursor += 1;
            continue;
        }
        while start > 0
            && !table_seeds[start - 1]
            && rows_are_adjacent(&rows[start - 1], &rows[start])
            && row_continues_table(&rows[start - 1], table_box, page_width)
        {
            start -= 1;
            table_box = union_boxes([table_box, rows[start].bbox]).unwrap_or(table_box);
        }
        ranges.push(NativeRowRange { start, end });
        cursor = index.max(end + 1);
    }
    ranges
}

#[derive(Clone, Debug)]
struct ColumnAnchor {
    position: f64,
    observations: usize,
    row_indexes: BTreeSet<usize>,
}

fn infer_table_cells(
    rows: &[NativeRow],
    candidates: &[NativeTextCandidate],
) -> Vec<NativeTextCell> {
    let tolerance = 2.0_f64.max(median(rows.iter().map(|row| row.height)) * 0.8);
    let mut anchors: Vec<ColumnAnchor> = Vec::new();
    for (row_index, row) in rows.iter().enumerate() {
        for candidate_index in &row.candidate_indexes {
            let x = f64::from(candidates[*candidate_index].bbox.left);
            let anchor_index = anchors
                .iter()
                .enumerate()
                .filter(|(_, anchor)| (anchor.position - x).abs() <= tolerance)
                .min_by(|left, right| {
                    (left.1.position - x)
                        .abs()
                        .partial_cmp(&(right.1.position - x).abs())
                        .unwrap_or(Ordering::Equal)
                        .then(left.0.cmp(&right.0))
                })
                .map(|(index, _)| index);
            let anchor_index = anchor_index.unwrap_or_else(|| {
                anchors.push(ColumnAnchor {
                    position: x,
                    observations: 0,
                    row_indexes: BTreeSet::new(),
                });
                anchors.len() - 1
            });
            let anchor = &mut anchors[anchor_index];
            anchor.position = (anchor.position * anchor.observations as f64 + x)
                / (anchor.observations + 1) as f64;
            anchor.observations += 1;
            anchor.row_indexes.insert(row_index);
        }
    }
    let minimum_support = 2_usize.max((rows.len() as f64 * 0.08).ceil() as usize);
    let mut supported = anchors
        .iter()
        .filter(|anchor| anchor.row_indexes.len() >= minimum_support)
        .cloned()
        .collect::<Vec<_>>();
    if supported.len() < 2 {
        let take = 2_usize.max(
            rows.iter()
                .map(|row| row.candidate_indexes.len())
                .max()
                .unwrap_or(0),
        );
        supported = anchors.clone();
        supported.sort_by(|left, right| {
            right
                .row_indexes
                .len()
                .cmp(&left.row_indexes.len())
                .then_with(|| {
                    left.position
                        .partial_cmp(&right.position)
                        .unwrap_or(Ordering::Equal)
                })
        });
        supported.truncate(take);
    }
    supported.sort_by(|left, right| {
        left.position
            .partial_cmp(&right.position)
            .unwrap_or(Ordering::Equal)
    });

    let mut cells = Vec::new();
    for (row_index, row) in rows.iter().enumerate() {
        let mut grouped: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        for candidate_index in &row.candidate_indexes {
            let x = f64::from(candidates[*candidate_index].bbox.left);
            let column = supported
                .iter()
                .enumerate()
                .min_by(|left, right| {
                    (left.1.position - x)
                        .abs()
                        .partial_cmp(&(right.1.position - x).abs())
                        .unwrap_or(Ordering::Equal)
                        .then(left.0.cmp(&right.0))
                })
                .map(|(index, _)| index)
                .unwrap_or(0);
            grouped.entry(column).or_default().push(*candidate_index);
        }
        for candidate_indexes in grouped.values_mut() {
            candidate_indexes.sort_by_key(|index| {
                (
                    candidates[*index].bbox.left,
                    candidates[*index].source_item_index,
                )
            });
        }
        for (&column, candidate_indexes) in &grouped {
            let Some(bbox) = union_boxes(
                candidate_indexes
                    .iter()
                    .map(|candidate_index| candidates[*candidate_index].bbox),
            ) else {
                continue;
            };
            let mut last_column = column;
            while last_column + 1 < supported.len()
                && !grouped.contains_key(&(last_column + 1))
                && supported[last_column + 1].position < f64::from(bbox.right) - tolerance
            {
                last_column += 1;
            }
            cells.push(NativeTextCell {
                row: row_index as u32,
                column: column as u32,
                row_span: 1,
                column_span: (last_column - column + 1) as u32,
                bbox,
                candidate_indexes: candidate_indexes.clone(),
            });
        }
    }
    cells.sort_by_key(|cell| (cell.row, cell.column));
    cells
}

fn paragraph_cells(
    rows: &[NativeRow],
    candidates: &[NativeTextCandidate],
) -> Vec<NativeTextCell> {
    rows.iter()
        .enumerate()
        .map(|(row_index, row)| {
            let mut candidate_indexes = row.candidate_indexes.clone();
            candidate_indexes.sort_by_key(|index| {
                (
                    candidates[*index].bbox.left,
                    candidates[*index].source_item_index,
                )
            });
            NativeTextCell {
                row: row_index as u32,
                column: 0,
                row_span: 1,
                column_span: 1,
                bbox: row.bbox,
                candidate_indexes,
            }
        })
        .collect()
}

#[derive(Clone, Debug)]
struct ObjectDraft {
    kind: StructuralObjectKind,
    rows: Vec<NativeRow>,
}

fn find_native_pdf_text_objects(
    candidates: &[NativeTextCandidate],
) -> Vec<NativeTextObject> {
    let mut rows: Vec<NativeRow> = Vec::new();
    let mut ordered_indexes = (0..candidates.len()).collect::<Vec<_>>();
    ordered_indexes.sort_by_key(|index| {
        let candidate = &candidates[*index];
        (
            std::cmp::Reverse(candidate.bbox.bottom),
            candidate.bbox.left,
            candidate.source_item_index,
        )
    });

    for candidate_index in ordered_indexes {
        let candidate = &candidates[candidate_index];
        let height = candidate.bbox.bottom - candidate.bbox.top;
        let center_y = f64::from(candidate.bbox.top + candidate.bbox.bottom) / 2.0;
        let row_index = rows.iter().position(|row| {
            (row.center_y() - center_y).abs()
                <= 2.0_f64.max(f64::from(row.height.min(height)) * 0.6)
        });
        if let Some(row_index) = row_index {
            let row = &mut rows[row_index];
            row.candidate_indexes.push(candidate_index);
            let center_sum = row
                .candidate_indexes
                .iter()
                .map(|index| {
                    let bbox = candidates[*index].bbox;
                    f64::from(bbox.top + bbox.bottom) / 2.0
                })
                .sum::<f64>();
            row.center_y = center_sum / row.candidate_indexes.len() as f64;
            row.height = row.height.max(height);
            row.bbox = union_boxes([row.bbox, candidate.bbox]).unwrap_or(row.bbox);
        } else {
            rows.push(NativeRow {
                center_y,
                height,
                bbox: candidate.bbox,
                candidate_indexes: vec![candidate_index],
            });
        }
    }

    rows.sort_by(|left, right| {
        right
            .center_y
            .partial_cmp(&left.center_y)
            .unwrap_or(Ordering::Equal)
    });
    for row in &mut rows {
        row.candidate_indexes
            .sort_by_key(|index| candidates[*index].bbox.left);
    }
    let table_ranges = find_table_ranges(&rows, candidates);
    let table_by_start = table_ranges
        .iter()
        .map(|range| (range.start, *range))
        .collect::<BTreeMap<_, _>>();
    let mut drafts = Vec::new();
    let mut row_index = 0;
    while row_index < rows.len() {
        if let Some(range) = table_by_start.get(&row_index) {
            drafts.push(ObjectDraft {
                kind: StructuralObjectKind::Table,
                rows: rows[range.start..=range.end].to_vec(),
            });
            row_index = range.end + 1;
            continue;
        }
        let start = row_index;
        row_index += 1;
        while row_index < rows.len()
            && !table_by_start.contains_key(&row_index)
            && rows_are_adjacent(&rows[row_index - 1], &rows[row_index])
        {
            row_index += 1;
        }
        drafts.push(ObjectDraft {
            kind: StructuralObjectKind::Paragraph,
            rows: rows[start..row_index].to_vec(),
        });
    }

    let mut index = 1;
    while index + 1 < drafts.len() {
        let page_width = union_boxes(candidates.iter().map(|candidate| candidate.bbox))
            .map(|bbox| f64::from(bbox.right - bbox.left))
            .unwrap_or(0.0);
        let continuation_box = union_boxes(drafts[index].rows.iter().map(|row| row.bbox));
        let continuation_row = drafts[index].rows.first().cloned();
        let previous_box = union_boxes(drafts[index - 1].rows.iter().map(|row| row.bbox));
        let next_box = union_boxes(drafts[index + 1].rows.iter().map(|row| row.bbox));
        let bridges_wide_table = previous_box
            .zip(continuation_box)
            .zip(next_box)
            .is_some_and(|((previous_box, continuation_box), next_box)| {
                let previous_width = f64::from(previous_box.right - previous_box.left);
                let next_width = f64::from(next_box.right - next_box.left);
                let width_similarity = previous_width.min(next_width)
                    / previous_width.max(next_width).max(1.0);
                let previous_gap = (previous_box.top - continuation_box.bottom).max(0);
                let next_gap = (continuation_box.top - next_box.bottom).max(0);
                drafts[index - 1].kind == StructuralObjectKind::Table
                    && drafts[index].kind == StructuralObjectKind::Paragraph
                    && drafts[index + 1].kind == StructuralObjectKind::Table
                    && drafts[index - 1].rows.len() >= 32
                    && previous_width >= page_width * 0.75
                    && next_width >= page_width * 0.75
                    && width_similarity >= 0.85
                    && previous_gap <= 18
                    && next_gap <= 18
                    && drafts[index].rows.iter().all(|row| {
                        row_aligns_with_established_table_column(
                            row,
                            &drafts[index - 1].rows,
                            candidates,
                        ) || row_continues_table(row, previous_box, page_width)
                    })
            });
        if bridges_wide_table {
            let continuation_rows = drafts[index].rows.clone();
            let following_rows = drafts[index + 1].rows.clone();
            drafts[index - 1].rows.extend(continuation_rows);
            drafts[index - 1].rows.extend(following_rows);
            drafts.remove(index + 1);
            drafts.remove(index);
            continue;
        }
        let belongs = continuation_row.as_ref().is_some_and(|continuation_row| {
            let Some(previous_box) = previous_box else {
                return false;
            };
            let Some(next_box) = next_box else {
                return false;
            };
            let overlaps_previous = continuation_row.bbox.bottom.min(previous_box.bottom)
                - continuation_row.bbox.top.max(previous_box.top);
            let overlaps_next = continuation_row.bbox.bottom.min(next_box.bottom)
                - continuation_row.bbox.top.max(next_box.top);
            drafts[index - 1].kind == StructuralObjectKind::Table
                && drafts[index].kind == StructuralObjectKind::Paragraph
                && drafts[index].rows.len() == 1
                && drafts[index + 1].kind == StructuralObjectKind::Table
                && overlaps_previous > 0
                && overlaps_next <= 0
                && row_aligns_with_established_table_column(
                    continuation_row,
                    &drafts[index - 1].rows,
                    candidates,
                )
        });
        if !belongs {
            index += 1;
            continue;
        }
        let continuation_row = continuation_row.expect("continuation was checked");
        drafts[index - 1].rows.push(continuation_row);
        drafts.remove(index);
    }

    drafts
        .into_iter()
        .enumerate()
        .filter_map(|(object_index, draft)| {
            let bbox = union_boxes(draft.rows.iter().map(|row| row.bbox))?;
            let cells = if draft.kind == StructuralObjectKind::Paragraph {
                paragraph_cells(&draft.rows, candidates)
            } else {
                infer_table_cells(&draft.rows, candidates)
            };
            Some(NativeTextObject {
                object_id: format!("native-object-{}", object_index + 1),
                kind: draft.kind,
                bbox,
                rows: draft
                    .rows
                    .iter()
                    .map(|row| row.candidate_indexes.clone())
                    .collect(),
                cells,
            })
        })
        .collect()
}

fn extract_native_pdf_text_segments(
    candidates: &[NativeTextCandidate],
    objects: &[NativeTextObject],
) -> Vec<NativePdfSegment> {
    let mut segments = Vec::new();
    for object in objects {
        for cell in &object.cells {
            for candidate_index in &cell.candidate_indexes {
                let candidate = &candidates[*candidate_index];
                segments.push(NativePdfSegment {
                    segment_id: format!("native-segment-{:06}", candidate.source_item_index + 1),
                    source_item_index: candidate.source_item_index,
                    bbox: candidate.bbox,
                    object_id: object.object_id.clone(),
                    object_kind: object.kind,
                    row: cell.row,
                    column: cell.column,
                    row_span: cell.row_span,
                    column_span: cell.column_span,
                    text: candidate.text.clone(),
                });
            }
        }
    }
    segments
}

pub fn build_native_pdf_artifact(
    items: &[PdfTextGeometryItem],
) -> Result<Option<NativePdfArtifact>, AssemblerError> {
    let candidates = items
        .iter()
        .enumerate()
        .filter_map(|(index, item)| candidate_from_item(item, index))
        .collect::<Vec<_>>();
    if candidates.is_empty() {
        return Ok(None);
    }
    let objects = find_native_pdf_text_objects(&candidates);
    let segments = extract_native_pdf_text_segments(&candidates, &objects);
    let layouts = objects
        .iter()
        .map(|object| SegmentObjectLayout {
            object_id: object.object_id.clone(),
            object_kind: object.kind,
            logical_row_count: object
                .cells
                .iter()
                .map(|cell| cell.row + cell.row_span)
                .max()
                .unwrap_or(1),
            logical_column_count: object
                .cells
                .iter()
                .map(|cell| cell.column + cell.column_span)
                .max()
                .unwrap_or(1),
        })
        .collect::<Vec<_>>();
    let assembler_segments = segments
        .iter()
        .map(|segment| AssemblerSegment {
            segment_id: segment.segment_id.clone(),
            object_id: segment.object_id.clone(),
            object_kind: segment.object_kind,
            row: segment.row,
            column: segment.column,
            row_span: segment.row_span,
            column_span: segment.column_span,
            text: segment.text.clone(),
        })
        .collect::<Vec<_>>();
    let assembled = assemble_segment_topology(
        AssemblerSource::TrustedPdfTextLayer,
        &layouts,
        &assembler_segments,
    )?;
    Ok(Some(NativePdfArtifact {
        objects,
        segments,
        assembled,
    }))
}

#[derive(Default)]
struct NativePdfSession {
    items: Vec<PdfTextGeometryItem>,
    rendered: Option<Vec<u8>>,
}

#[derive(Default)]
struct NativePdfRegistry {
    next_handle: u32,
    sessions: BTreeMap<u32, NativePdfSession>,
}

fn native_pdf_registry() -> &'static std::sync::Mutex<NativePdfRegistry> {
    static REGISTRY: std::sync::OnceLock<std::sync::Mutex<NativePdfRegistry>> =
        std::sync::OnceLock::new();
    REGISTRY.get_or_init(|| {
        std::sync::Mutex::new(NativePdfRegistry {
            next_handle: 1,
            sessions: BTreeMap::new(),
        })
    })
}

unsafe fn native_pdf_utf8(pointer: *const u8, length: u32) -> Result<String, ()> {
    if length == 0 {
        return Ok(String::new());
    }
    if pointer.is_null() {
        return Err(());
    }
    let bytes = unsafe { std::slice::from_raw_parts(pointer, length as usize) };
    std::str::from_utf8(bytes)
        .map(str::to_owned)
        .map_err(|_| ())
}

fn push_json_string(output: &mut String, value: &str) {
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            '\u{08}' => output.push_str("\\b"),
            '\u{0c}' => output.push_str("\\f"),
            character if character <= '\u{1f}' => {
                output.push_str(&format!("\\u{:04x}", character as u32));
            }
            character => output.push(character),
        }
    }
    output.push('"');
}

fn push_json_box(output: &mut String, bbox: PdfTextBox) {
    output.push_str("{\"left\":");
    output.push_str(&bbox.left.to_string());
    output.push_str(",\"top\":");
    output.push_str(&bbox.top.to_string());
    output.push_str(",\"right\":");
    output.push_str(&bbox.right.to_string());
    output.push_str(",\"bottom\":");
    output.push_str(&bbox.bottom.to_string());
    output.push('}');
}

fn structural_kind_name(kind: StructuralObjectKind) -> &'static str {
    match kind {
        StructuralObjectKind::Paragraph => "paragraph",
        StructuralObjectKind::Table => "table",
        StructuralObjectKind::SmallTable => "small_table",
    }
}

fn push_json_indexes(output: &mut String, indexes: &[usize]) {
    output.push('[');
    for (index, value) in indexes.iter().enumerate() {
        if index > 0 {
            output.push(',');
        }
        output.push_str(&value.to_string());
    }
    output.push(']');
}

fn render_native_pdf_parts(artifact: &NativePdfArtifact) -> Vec<u8> {
    let mut output = String::from("{\"objects\":[");
    for (object_index, object) in artifact.objects.iter().enumerate() {
        if object_index > 0 {
            output.push(',');
        }
        output.push_str("{\"objectId\":");
        push_json_string(&mut output, &object.object_id);
        output.push_str(",\"kind\":");
        push_json_string(&mut output, structural_kind_name(object.kind));
        output.push_str(",\"bbox\":");
        push_json_box(&mut output, object.bbox);
        output.push_str(",\"rows\":[");
        for (row_index, row) in object.rows.iter().enumerate() {
            if row_index > 0 {
                output.push(',');
            }
            push_json_indexes(&mut output, row);
        }
        output.push_str("],\"cells\":[");
        for (cell_index, cell) in object.cells.iter().enumerate() {
            if cell_index > 0 {
                output.push(',');
            }
            output.push_str("{\"row\":");
            output.push_str(&cell.row.to_string());
            output.push_str(",\"column\":");
            output.push_str(&cell.column.to_string());
            output.push_str(",\"rowSpan\":");
            output.push_str(&cell.row_span.to_string());
            output.push_str(",\"columnSpan\":");
            output.push_str(&cell.column_span.to_string());
            output.push_str(",\"bbox\":");
            push_json_box(&mut output, cell.bbox);
            output.push_str(",\"candidateIndexes\":");
            push_json_indexes(&mut output, &cell.candidate_indexes);
            output.push('}');
        }
        output.push_str("]}");
    }
    output.push_str("],\"segments\":[");
    for (segment_index, segment) in artifact.segments.iter().enumerate() {
        if segment_index > 0 {
            output.push(',');
        }
        output.push_str("{\"segmentId\":");
        push_json_string(&mut output, &segment.segment_id);
        output.push_str(",\"sourceItemIndex\":");
        output.push_str(&segment.source_item_index.to_string());
        output.push_str(",\"bbox\":");
        push_json_box(&mut output, segment.bbox);
        output.push_str(",\"topology\":{\"object_id\":");
        push_json_string(&mut output, &segment.object_id);
        output.push_str(",\"object_kind\":");
        push_json_string(&mut output, structural_kind_name(segment.object_kind));
        output.push_str(",\"row\":");
        output.push_str(&segment.row.to_string());
        output.push_str(",\"column\":");
        output.push_str(&segment.column.to_string());
        output.push_str(",\"row_span\":");
        output.push_str(&segment.row_span.to_string());
        output.push_str(",\"column_span\":");
        output.push_str(&segment.column_span.to_string());
        output.push_str("},\"text\":");
        push_json_string(&mut output, &segment.text);
        output.push('}');
    }
    output.push_str("]}");
    output.into_bytes()
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_pdf_native_begin() -> u32 {
    let Ok(mut registry) = native_pdf_registry().lock() else {
        return 0;
    };
    let handle = registry.next_handle;
    registry.next_handle = registry.next_handle.wrapping_add(1).max(1);
    registry.sessions.insert(handle, NativePdfSession::default());
    handle
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_pdf_native_add_item(
    handle: u32,
    text: *const u8,
    text_length: u32,
    width: f64,
    height: f64,
    has_height: u32,
    transform_0: f64,
    transform_1: f64,
    transform_2: f64,
    transform_3: f64,
    transform_4: f64,
    transform_5: f64,
) -> i32 {
    let Ok(text) = (unsafe { native_pdf_utf8(text, text_length) }) else {
        return -1;
    };
    let Ok(mut registry) = native_pdf_registry().lock() else {
        return -2;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return -3;
    };
    session.items.push(PdfTextGeometryItem {
        text,
        width,
        height: (has_height != 0).then_some(height),
        transform: [
            transform_0,
            transform_1,
            transform_2,
            transform_3,
            transform_4,
            transform_5,
        ],
    });
    session.rendered = None;
    0
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_pdf_native_build(handle: u32) -> i32 {
    let items = {
        let Ok(registry) = native_pdf_registry().lock() else {
            return -1;
        };
        let Some(session) = registry.sessions.get(&handle) else {
            return -2;
        };
        session.items.clone()
    };
    let rendered = match build_native_pdf_artifact(&items) {
        Ok(Some(artifact)) => render_native_pdf_parts(&artifact),
        Ok(None) => b"null".to_vec(),
        Err(_) => return -3,
    };
    let Ok(mut registry) = native_pdf_registry().lock() else {
        return -1;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return -2;
    };
    session.rendered = Some(rendered);
    0
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_pdf_native_render_length(handle: u32) -> u32 {
    native_pdf_registry()
        .lock()
        .ok()
        .and_then(|registry| registry.sessions.get(&handle).and_then(|session| session.rendered.as_ref().map(Vec::len)))
        .and_then(|length| u32::try_from(length).ok())
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_pdf_native_render_copy(
    handle: u32,
    pointer: *mut u8,
    capacity: u32,
) -> i32 {
    let Ok(registry) = native_pdf_registry().lock() else {
        return -1;
    };
    let Some(rendered) = registry.sessions.get(&handle).and_then(|session| session.rendered.as_ref()) else {
        return -2;
    };
    if pointer.is_null() || (capacity as usize) < rendered.len() {
        return -3;
    }
    unsafe { std::ptr::copy_nonoverlapping(rendered.as_ptr(), pointer, rendered.len()) };
    i32::try_from(rendered.len()).unwrap_or(-4)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_pdf_native_drop(handle: u32) -> i32 {
    let Ok(mut registry) = native_pdf_registry().lock() else {
        return -1;
    };
    if registry.sessions.remove(&handle).is_some() {
        0
    } else {
        -2
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn item(text: &str, x: f64, baseline: f64, width: f64, height: f64) -> PdfTextGeometryItem {
        PdfTextGeometryItem {
            text: text.to_owned(),
            width,
            height: Some(height),
            transform: [1.0, 0.0, 0.0, 1.0, x, baseline],
        }
    }

    #[test]
    fn owns_bbox_object_detection_and_segment_extraction() {
        let artifact = build_native_pdf_artifact(&[
            item("A", 10.0, 100.0, 20.0, 10.0),
            item("B", 80.0, 100.0, 20.0, 10.0),
            item("C", 10.0, 70.0, 20.0, 10.0),
            item("D", 80.0, 70.0, 20.0, 10.0),
        ])
        .unwrap()
        .unwrap();
        assert_eq!(artifact.objects[0].kind, StructuralObjectKind::Table);
        assert_eq!(artifact.segments.len(), 4);
        assert_eq!(
            artifact.assembled.markdown,
            "| A | B |\n| --- | --- |\n| C | D |"
        );
    }

    #[test]
    fn empty_items_do_not_manufacture_an_artifact() {
        assert!(build_native_pdf_artifact(&[]).unwrap().is_none());
        assert!(
            build_native_pdf_artifact(&[item("   ", 0.0, 0.0, 1.0, 1.0)])
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn separates_headers_and_preserves_merged_table_rows() {
        let artifact = build_native_pdf_artifact(&[
            item("Header", 300.0, 140.0, 60.0, 10.0),
            item("A", 10.0, 110.0, 20.0, 10.0),
            item("B", 50.0, 110.0, 20.0, 10.0),
            item("C", 90.0, 110.0, 20.0, 10.0),
            item("D", 10.0, 90.0, 20.0, 10.0),
            item("F", 90.0, 90.0, 20.0, 10.0),
            item("Section", 10.0, 70.0, 100.0, 10.0),
            item("G", 10.0, 50.0, 20.0, 10.0),
            item("H", 50.0, 50.0, 20.0, 10.0),
            item("I", 90.0, 50.0, 20.0, 10.0),
            item("Footer", 300.0, 10.0, 60.0, 10.0),
        ])
        .unwrap()
        .unwrap();
        assert_eq!(
            artifact
                .objects
                .iter()
                .map(|object| object.kind)
                .collect::<Vec<_>>(),
            vec![
                StructuralObjectKind::Paragraph,
                StructuralObjectKind::Table,
                StructuralObjectKind::Paragraph,
            ]
        );
        let table = &artifact.objects[1];
        assert_eq!(table.rows.len(), 4);
        let merged = table.cells.iter().find(|cell| {
            cell.candidate_indexes.iter().any(|candidate_index| {
                artifact.segments.iter().any(|segment| {
                    segment.source_item_index == *candidate_index && segment.text == "Section"
                })
            })
        });
        assert_eq!(merged.map(|cell| (cell.column, cell.column_span)), Some((0, 3)));
        assert_eq!(
            artifact
                .segments
                .iter()
                .filter(|segment| matches!(segment.text.as_str(), "D" | "F"))
                .map(|segment| segment.column)
                .collect::<Vec<_>>(),
            vec![0, 2]
        );
        assert_eq!(artifact.segments.len(), 11);
    }

    #[test]
    fn overlapping_glyph_boxes_do_not_create_row_spans() {
        let artifact = build_native_pdf_artifact(&[
            item("A", 10.0, 110.0, 20.0, 16.0),
            item("B", 90.0, 110.0, 20.0, 16.0),
            item("C", 10.0, 100.0, 20.0, 16.0),
            item("D", 90.0, 100.0, 20.0, 16.0),
        ])
        .unwrap()
        .unwrap();
        assert!(artifact.segments.iter().all(|segment| segment.row_span == 1));
    }

    #[test]
    fn continuation_stays_with_the_preceding_table() {
        let artifact = build_native_pdf_artifact(&[
            item("A", 10.0, 150.0, 20.0, 10.0),
            item("B", 50.0, 150.0, 20.0, 10.0),
            item("C", 90.0, 150.0, 20.0, 10.0),
            item("D", 10.0, 130.0, 20.0, 10.0),
            item("E", 50.0, 130.0, 20.0, 10.0),
            item("F", 90.0, 130.0, 20.0, 10.0),
            item("continued middle cell", 50.0, 121.0, 20.0, 10.0),
            item("G", 10.0, 70.0, 20.0, 10.0),
            item("H", 50.0, 70.0, 20.0, 10.0),
            item("I", 90.0, 70.0, 20.0, 10.0),
            item("J", 10.0, 50.0, 20.0, 10.0),
            item("K", 50.0, 50.0, 20.0, 10.0),
            item("L", 90.0, 50.0, 20.0, 10.0),
        ])
        .unwrap()
        .unwrap();
        assert_eq!(
            artifact
                .objects
                .iter()
                .map(|object| object.kind)
                .collect::<Vec<_>>(),
            vec![StructuralObjectKind::Table, StructuralObjectKind::Table]
        );
        let continuation = artifact
            .segments
            .iter()
            .find(|segment| segment.text == "continued middle cell")
            .unwrap();
        assert_eq!(continuation.object_id, "native-object-1");
        assert_eq!(continuation.column, 1);
        assert_eq!(artifact.assembled.objects.len(), 2);
        assert!(artifact.assembled.objects.iter().all(|object| {
            object
                .cells
                .iter()
                .all(|cell| cell.text.is_empty() || !cell.segment_ids.is_empty())
        }));
    }
}
