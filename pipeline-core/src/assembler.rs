use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Mutex, OnceLock};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AssemblerSource {
    TrustedPdfTextLayer,
    RasterGeometry,
}

impl TryFrom<u32> for AssemblerSource {
    type Error = ();

    fn try_from(value: u32) -> Result<Self, Self::Error> {
        match value {
            0 => Ok(Self::TrustedPdfTextLayer),
            1 => Ok(Self::RasterGeometry),
            _ => Err(()),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StructuralObjectKind {
    Paragraph,
    Table,
    SmallTable,
}

impl StructuralObjectKind {
    fn is_table(self) -> bool {
        matches!(self, Self::Table | Self::SmallTable)
    }
}

impl TryFrom<u32> for StructuralObjectKind {
    type Error = ();

    fn try_from(value: u32) -> Result<Self, Self::Error> {
        match value {
            0 => Ok(Self::Paragraph),
            1 => Ok(Self::Table),
            2 => Ok(Self::SmallTable),
            _ => Err(()),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SegmentObjectLayout {
    pub object_id: String,
    pub object_kind: StructuralObjectKind,
    pub logical_row_count: u32,
    pub logical_column_count: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AssemblerSegment {
    pub segment_id: String,
    pub object_id: String,
    pub object_kind: StructuralObjectKind,
    pub row: u32,
    pub column: u32,
    pub row_span: u32,
    pub column_span: u32,
    pub text: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StructuralCell {
    pub row: u32,
    pub column: u32,
    pub row_span: u32,
    pub column_span: u32,
    pub segment_ids: Vec<String>,
    pub text: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StructuralObject {
    pub object_id: String,
    pub kind: StructuralObjectKind,
    pub logical_row_count: u32,
    pub logical_column_count: u32,
    pub cells: Vec<StructuralCell>,
    pub markdown: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TopologyRenderArtifact {
    pub objects: Vec<StructuralObject>,
    pub markdown: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AssemblerError {
    DuplicateLayout,
    DuplicateSegment,
    MissingObject,
    MixedObjectKinds,
    InvalidSpan,
    SegmentOutsideLayout,
    IncompatibleAnchorSpans,
    AnchorOverlap,
}

fn format_markdown_table_cell(value: &str) -> String {
    let trimmed = value.trim();
    let Some((left, right)) = trimmed.split_once('/') else {
        return value.to_owned();
    };
    if left.trim().bytes().all(|byte| byte.is_ascii_digit())
        && !left.trim().is_empty()
        && right.trim().bytes().all(|byte| byte.is_ascii_digit())
        && !right.trim().is_empty()
    {
        format!("{} / {}", left.trim(), right.trim())
    } else {
        value.to_owned()
    }
}

fn render_row(row: &[String]) -> String {
    format!(
        "| {} |",
        row.iter()
            .map(|value| format_markdown_table_cell(value).replace('|', "\\|"))
            .collect::<Vec<_>>()
            .join(" | ")
    )
}

fn markdown_for_object(
    kind: StructuralObjectKind,
    cells: &[StructuralCell],
    logical_row_count: u32,
    logical_column_count: u32,
) -> String {
    let cells_by_coordinate = cells
        .iter()
        .map(|cell| ((cell.row, cell.column), cell))
        .collect::<BTreeMap<_, _>>();
    let rows = (0..logical_row_count)
        .map(|row| {
            (0..logical_column_count)
                .map(|column| {
                    cells_by_coordinate
                        .get(&(row, column))
                        .map_or_else(String::new, |cell| cell.text.clone())
                })
                .collect::<Vec<_>>()
        })
        .collect::<Vec<_>>();
    if kind.is_table() {
        let Some(first) = rows.first() else {
            return String::new();
        };
        let mut lines = vec![
            render_row(first),
            render_row(&vec!["---".to_owned(); first.len()]),
        ];
        lines.extend(rows.iter().skip(1).map(|row| render_row(row)));
        return lines.join("\n");
    }
    rows.into_iter()
        .map(|row| {
            row.into_iter()
                .filter(|value| !value.is_empty())
                .collect::<Vec<_>>()
                .join(" ")
                .trim()
                .to_owned()
        })
        .filter(|row| !row.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn materialize_object_cells(
    kind: StructuralObjectKind,
    segments: &[AssemblerSegment],
) -> Result<Vec<StructuralCell>, AssemblerError> {
    let mut anchors = BTreeMap::<(u32, u32), StructuralCell>::new();
    for segment in segments {
        let coordinate = (segment.row, segment.column);
        if let Some(existing) = anchors.get_mut(&coordinate) {
            if existing.row_span != segment.row_span || existing.column_span != segment.column_span
            {
                return Err(AssemblerError::IncompatibleAnchorSpans);
            }
            existing.segment_ids.push(segment.segment_id.clone());
            let text = segment.text.trim();
            if !text.is_empty() {
                if !existing.text.is_empty() {
                    existing.text.push(' ');
                }
                existing.text.push_str(text);
            }
            continue;
        }
        anchors.insert(
            coordinate,
            StructuralCell {
                row: segment.row,
                column: segment.column,
                row_span: segment.row_span,
                column_span: segment.column_span,
                segment_ids: vec![segment.segment_id.clone()],
                text: segment.text.trim().to_owned(),
            },
        );
    }
    let mut ordered = anchors.into_values().collect::<Vec<_>>();
    if !kind.is_table() {
        return Ok(ordered);
    }

    for index in 0..ordered.len() {
        for other_index in 0..ordered.len() {
            if index == other_index {
                continue;
            }
            let other = &ordered[other_index];
            let covers_other = other.row >= ordered[index].row
                && other.row < ordered[index].row.saturating_add(ordered[index].row_span)
                && other.column >= ordered[index].column
                && other.column
                    < ordered[index]
                        .column
                        .saturating_add(ordered[index].column_span);
            if !covers_other {
                continue;
            }
            let row_limit = other.row - ordered[index].row;
            let column_limit = other.column - ordered[index].column;
            if row_limit == 0 && column_limit > 0 {
                ordered[index].column_span = ordered[index].column_span.min(column_limit);
            } else if column_limit == 0 && row_limit > 0 {
                ordered[index].row_span = ordered[index].row_span.min(row_limit);
            } else if row_limit > 0 && column_limit > 0 {
                let row_clip_area = row_limit.saturating_mul(ordered[index].column_span);
                let column_clip_area = ordered[index].row_span.saturating_mul(column_limit);
                if row_clip_area >= column_clip_area {
                    ordered[index].row_span = ordered[index].row_span.min(row_limit);
                } else {
                    ordered[index].column_span = ordered[index].column_span.min(column_limit);
                }
            }
        }
    }

    let mut occupied = BTreeMap::<(u32, u32), (u32, u32)>::new();
    for cell in &mut ordered {
        let anchor = (cell.row, cell.column);
        loop {
            let conflict = (cell.row..cell.row.saturating_add(cell.row_span)).find_map(|row| {
                (cell.column..cell.column.saturating_add(cell.column_span)).find_map(|column| {
                    occupied
                        .get(&(row, column))
                        .filter(|previous| **previous != anchor)
                        .map(|_| (row, column))
                })
            });
            let Some((conflict_row, conflict_column)) = conflict else {
                break;
            };
            let row_limit = conflict_row - cell.row;
            let column_limit = conflict_column - cell.column;
            if row_limit == 0 && column_limit == 0 {
                return Err(AssemblerError::AnchorOverlap);
            }
            if row_limit == 0 {
                cell.column_span = column_limit.max(1);
            } else if column_limit == 0 {
                cell.row_span = row_limit.max(1);
            } else if row_limit.saturating_mul(cell.column_span)
                >= cell.row_span.saturating_mul(column_limit)
            {
                cell.row_span = row_limit;
            } else {
                cell.column_span = column_limit;
            }
        }
        for row in cell.row..cell.row.saturating_add(cell.row_span) {
            for column in cell.column..cell.column.saturating_add(cell.column_span) {
                occupied.insert((row, column), anchor);
            }
        }
    }
    Ok(ordered)
}

fn attach_leading_paragraph_headers(objects: Vec<StructuralObject>) -> Vec<StructuralObject> {
    let mut merged = Vec::new();
    let mut index = 0;
    while index < objects.len() {
        let paragraph = &objects[index];
        let table = objects.get(index + 1);
        let Some(table) = table else {
            merged.push(paragraph.clone());
            break;
        };
        let header_values = paragraph
            .cells
            .first()
            .map(|cell| cell.text.split_whitespace().collect::<Vec<_>>())
            .unwrap_or_default();
        if paragraph.kind != StructuralObjectKind::Paragraph
            || paragraph.cells.len() != 1
            || !table.kind.is_table()
            || table.logical_column_count < 2
            || header_values.len() != table.logical_column_count as usize
        {
            merged.push(paragraph.clone());
            index += 1;
            continue;
        }
        let mut cells = header_values
            .into_iter()
            .enumerate()
            .map(|(column, text)| StructuralCell {
                row: 0,
                column: column as u32,
                row_span: 1,
                column_span: 1,
                segment_ids: if column == 0 {
                    paragraph.cells[0].segment_ids.clone()
                } else {
                    Vec::new()
                },
                text: text.to_owned(),
            })
            .collect::<Vec<_>>();
        cells.extend(table.cells.iter().cloned().map(|mut cell| {
            cell.row = cell.row.saturating_add(1);
            cell
        }));
        let logical_row_count = table.logical_row_count.saturating_add(1);
        merged.push(StructuralObject {
            object_id: table.object_id.clone(),
            kind: table.kind,
            logical_row_count,
            logical_column_count: table.logical_column_count,
            markdown: markdown_for_object(
                table.kind,
                &cells,
                logical_row_count,
                table.logical_column_count,
            ),
            cells,
        });
        index += 2;
    }
    merged
}

pub fn assemble_segment_topology(
    source: AssemblerSource,
    layouts: &[SegmentObjectLayout],
    segments: &[AssemblerSegment],
) -> Result<TopologyRenderArtifact, AssemblerError> {
    let mut layout_ids = BTreeSet::new();
    for layout in layouts {
        if layout.object_id.is_empty() || !layout_ids.insert(layout.object_id.clone()) {
            return Err(AssemblerError::DuplicateLayout);
        }
    }
    let layout_by_id = layouts
        .iter()
        .map(|layout| (layout.object_id.as_str(), layout))
        .collect::<BTreeMap<_, _>>();
    let mut segment_ids = BTreeSet::new();
    for segment in segments {
        if segment.segment_id.is_empty() || !segment_ids.insert(segment.segment_id.clone()) {
            return Err(AssemblerError::DuplicateSegment);
        }
        if segment.object_id.is_empty() || segment.row_span == 0 || segment.column_span == 0 {
            return Err(AssemblerError::InvalidSpan);
        }
        if let Some(layout) = layout_by_id.get(segment.object_id.as_str()) {
            if layout.object_kind != segment.object_kind {
                return Err(AssemblerError::MixedObjectKinds);
            }
            if segment.row.saturating_add(segment.row_span) > layout.logical_row_count
                || segment.column.saturating_add(segment.column_span) > layout.logical_column_count
            {
                return Err(AssemblerError::SegmentOutsideLayout);
            }
        }
    }

    let mut ordered_ids = layouts
        .iter()
        .map(|layout| layout.object_id.clone())
        .collect::<Vec<_>>();
    for segment in segments {
        if !ordered_ids.contains(&segment.object_id) {
            ordered_ids.push(segment.object_id.clone());
        }
    }
    let mut objects = Vec::new();
    for object_id in ordered_ids {
        let mut object_segments = segments
            .iter()
            .filter(|segment| segment.object_id == object_id)
            .cloned()
            .collect::<Vec<_>>();
        object_segments.sort_by(|left, right| {
            left.row
                .cmp(&right.row)
                .then(left.column.cmp(&right.column))
                .then(left.segment_id.cmp(&right.segment_id))
        });
        let layout = layout_by_id.get(object_id.as_str()).copied();
        let kind = layout
            .map(|layout| layout.object_kind)
            .or_else(|| object_segments.first().map(|segment| segment.object_kind))
            .ok_or(AssemblerError::MissingObject)?;
        if object_segments
            .iter()
            .any(|segment| segment.object_kind != kind)
        {
            return Err(AssemblerError::MixedObjectKinds);
        }
        let cells = materialize_object_cells(kind, &object_segments)?;
        let logical_row_count = layout.map_or_else(
            || {
                cells
                    .iter()
                    .map(|cell| cell.row.saturating_add(cell.row_span))
                    .max()
                    .unwrap_or(0)
            },
            |layout| layout.logical_row_count,
        );
        let logical_column_count = layout.map_or_else(
            || {
                cells
                    .iter()
                    .map(|cell| cell.column.saturating_add(cell.column_span))
                    .max()
                    .unwrap_or(0)
            },
            |layout| layout.logical_column_count,
        );
        objects.push(StructuralObject {
            object_id,
            kind,
            logical_row_count,
            logical_column_count,
            markdown: markdown_for_object(kind, &cells, logical_row_count, logical_column_count),
            cells,
        });
    }
    if source == AssemblerSource::RasterGeometry {
        objects = attach_leading_paragraph_headers(objects);
    }
    let markdown = objects
        .iter()
        .map(|object| object.markdown.as_str())
        .filter(|markdown| !markdown.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n");
    Ok(TopologyRenderArtifact { objects, markdown })
}

#[derive(Debug)]
struct AssemblerSession {
    source: AssemblerSource,
    layouts: Vec<SegmentObjectLayout>,
    segments: Vec<AssemblerSegment>,
    artifact: Option<TopologyRenderArtifact>,
    rendered: Option<Vec<u8>>,
}

#[derive(Debug)]
struct Registry {
    next_handle: u32,
    sessions: BTreeMap<u32, AssemblerSession>,
}

fn registry() -> &'static Mutex<Registry> {
    static REGISTRY: OnceLock<Mutex<Registry>> = OnceLock::new();
    REGISTRY.get_or_init(|| {
        Mutex::new(Registry {
            next_handle: 1,
            sessions: BTreeMap::new(),
        })
    })
}

unsafe fn utf8_from_raw(pointer: *const u8, length: u32) -> Result<String, ()> {
    if pointer.is_null() && length > 0 {
        return Err(());
    }
    let bytes = if length == 0 {
        &[]
    } else {
        // SAFETY: the caller guarantees a readable allocation of `length` bytes.
        unsafe { std::slice::from_raw_parts(pointer, length as usize) }
    };
    std::str::from_utf8(bytes)
        .map(str::to_owned)
        .map_err(|_| ())
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_assembler_begin(source: u32) -> u32 {
    let Ok(source) = AssemblerSource::try_from(source) else {
        return 0;
    };
    let Ok(mut registry) = registry().lock() else {
        return 0;
    };
    let handle = registry.next_handle;
    registry.next_handle = registry.next_handle.wrapping_add(1).max(1);
    registry.sessions.insert(
        handle,
        AssemblerSession {
            source,
            layouts: Vec::new(),
            segments: Vec::new(),
            artifact: None,
            rendered: None,
        },
    );
    handle
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_assembler_add_layout(
    handle: u32,
    object_id: *const u8,
    object_id_length: u32,
    object_kind: u32,
    logical_row_count: u32,
    logical_column_count: u32,
) -> i32 {
    let Ok(object_id) = (unsafe { utf8_from_raw(object_id, object_id_length) }) else {
        return -1;
    };
    let Ok(object_kind) = StructuralObjectKind::try_from(object_kind) else {
        return -2;
    };
    let Ok(mut registry) = registry().lock() else {
        return -3;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return -4;
    };
    if object_id.is_empty()
        || session
            .layouts
            .iter()
            .any(|layout| layout.object_id == object_id)
    {
        return -5;
    }
    session.layouts.push(SegmentObjectLayout {
        object_id,
        object_kind,
        logical_row_count,
        logical_column_count,
    });
    session.artifact = None;
    session.rendered = None;
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_assembler_add_segment(
    handle: u32,
    segment_id: *const u8,
    segment_id_length: u32,
    object_id: *const u8,
    object_id_length: u32,
    object_kind: u32,
    row: u32,
    column: u32,
    row_span: u32,
    column_span: u32,
    text: *const u8,
    text_length: u32,
) -> i32 {
    let Ok(segment_id) = (unsafe { utf8_from_raw(segment_id, segment_id_length) }) else {
        return -1;
    };
    let Ok(object_id) = (unsafe { utf8_from_raw(object_id, object_id_length) }) else {
        return -2;
    };
    let Ok(text) = (unsafe { utf8_from_raw(text, text_length) }) else {
        return -3;
    };
    let Ok(object_kind) = StructuralObjectKind::try_from(object_kind) else {
        return -4;
    };
    let Ok(mut registry) = registry().lock() else {
        return -5;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return -6;
    };
    if segment_id.is_empty()
        || session
            .segments
            .iter()
            .any(|segment| segment.segment_id == segment_id)
        || object_id.is_empty()
        || row_span == 0
        || column_span == 0
    {
        return -7;
    }
    session.segments.push(AssemblerSegment {
        segment_id,
        object_id,
        object_kind,
        row,
        column,
        row_span,
        column_span,
        text,
    });
    session.artifact = None;
    session.rendered = None;
    0
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_assembler_render_length(handle: u32) -> u32 {
    let Ok(mut registry) = registry().lock() else {
        return 0;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return 0;
    };
    let Ok(artifact) =
        assemble_segment_topology(session.source, &session.layouts, &session.segments)
    else {
        return 0;
    };
    let rendered = artifact.markdown.as_bytes().to_vec();
    let length = rendered.len() as u32;
    session.artifact = Some(artifact);
    session.rendered = Some(rendered);
    length
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_assembler_render_copy(
    handle: u32,
    output: *mut u8,
    capacity: u32,
) -> i32 {
    if output.is_null() && capacity > 0 {
        return -1;
    }
    let Ok(registry) = registry().lock() else {
        return -2;
    };
    let Some(rendered) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.rendered.as_ref())
    else {
        return -3;
    };
    if (capacity as usize) < rendered.len() {
        return -4;
    }
    if !rendered.is_empty() {
        // SAFETY: capacity was validated and source and destination do not overlap.
        unsafe { std::ptr::copy_nonoverlapping(rendered.as_ptr(), output, rendered.len()) };
    }
    rendered.len() as i32
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_assembler_drop(handle: u32) -> i32 {
    let Ok(mut registry) = registry().lock() else {
        return -1;
    };
    if registry.sessions.remove(&handle).is_some() {
        0
    } else {
        -2
    }
}

fn object_kind_code(kind: StructuralObjectKind) -> i32 {
    match kind {
        StructuralObjectKind::Paragraph => 0,
        StructuralObjectKind::Table => 1,
        StructuralObjectKind::SmallTable => 2,
    }
}

fn copy_bytes(bytes: &[u8], output: *mut u8, capacity: u32) -> i32 {
    if output.is_null() && capacity > 0 {
        return -1;
    }
    if (capacity as usize) < bytes.len() {
        return -2;
    }
    if !bytes.is_empty() {
        // SAFETY: capacity was validated and source and destination do not overlap.
        unsafe { std::ptr::copy_nonoverlapping(bytes.as_ptr(), output, bytes.len()) };
    }
    bytes.len() as i32
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_assembler_object_count(handle: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.artifact.as_ref())
                .map(|artifact| artifact.objects.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_assembler_object_field(handle: u32, object: u32, field: u32) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(object) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.artifact.as_ref())
        .and_then(|artifact| artifact.objects.get(object as usize))
    else {
        return -2;
    };
    match field {
        0 => object_kind_code(object.kind),
        1 => i32::try_from(object.logical_row_count).unwrap_or(i32::MAX),
        2 => i32::try_from(object.logical_column_count).unwrap_or(i32::MAX),
        3 => i32::try_from(object.cells.len()).unwrap_or(i32::MAX),
        _ => -3,
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_assembler_object_id_length(handle: u32, object: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.artifact.as_ref())
                .and_then(|artifact| artifact.objects.get(object as usize))
                .map(|object| object.object_id.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_assembler_object_id_copy(
    handle: u32,
    object: u32,
    output: *mut u8,
    capacity: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -3;
    };
    let Some(object) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.artifact.as_ref())
        .and_then(|artifact| artifact.objects.get(object as usize))
    else {
        return -4;
    };
    copy_bytes(object.object_id.as_bytes(), output, capacity)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_assembler_object_markdown_length(handle: u32, object: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.artifact.as_ref())
                .and_then(|artifact| artifact.objects.get(object as usize))
                .map(|object| object.markdown.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_assembler_object_markdown_copy(
    handle: u32,
    object: u32,
    output: *mut u8,
    capacity: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -3;
    };
    let Some(object) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.artifact.as_ref())
        .and_then(|artifact| artifact.objects.get(object as usize))
    else {
        return -4;
    };
    copy_bytes(object.markdown.as_bytes(), output, capacity)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_assembler_cell_field(
    handle: u32,
    object: u32,
    cell: u32,
    field: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(cell) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.artifact.as_ref())
        .and_then(|artifact| artifact.objects.get(object as usize))
        .and_then(|object| object.cells.get(cell as usize))
    else {
        return -2;
    };
    match field {
        0 => i32::try_from(cell.row).unwrap_or(i32::MAX),
        1 => i32::try_from(cell.column).unwrap_or(i32::MAX),
        2 => i32::try_from(cell.row_span).unwrap_or(i32::MAX),
        3 => i32::try_from(cell.column_span).unwrap_or(i32::MAX),
        4 => i32::try_from(cell.segment_ids.len()).unwrap_or(i32::MAX),
        _ => -3,
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_assembler_cell_text_length(handle: u32, object: u32, cell: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.artifact.as_ref())
                .and_then(|artifact| artifact.objects.get(object as usize))
                .and_then(|object| object.cells.get(cell as usize))
                .map(|cell| cell.text.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_assembler_cell_text_copy(
    handle: u32,
    object: u32,
    cell: u32,
    output: *mut u8,
    capacity: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -3;
    };
    let Some(cell) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.artifact.as_ref())
        .and_then(|artifact| artifact.objects.get(object as usize))
        .and_then(|object| object.cells.get(cell as usize))
    else {
        return -4;
    };
    copy_bytes(cell.text.as_bytes(), output, capacity)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_assembler_cell_segment_id_length(
    handle: u32,
    object: u32,
    cell: u32,
    segment: u32,
) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.artifact.as_ref())
                .and_then(|artifact| artifact.objects.get(object as usize))
                .and_then(|object| object.cells.get(cell as usize))
                .and_then(|cell| cell.segment_ids.get(segment as usize))
                .map(|segment_id| segment_id.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_assembler_cell_segment_id_copy(
    handle: u32,
    object: u32,
    cell: u32,
    segment: u32,
    output: *mut u8,
    capacity: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -3;
    };
    let Some(segment_id) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.artifact.as_ref())
        .and_then(|artifact| artifact.objects.get(object as usize))
        .and_then(|object| object.cells.get(cell as usize))
        .and_then(|cell| cell.segment_ids.get(segment as usize))
    else {
        return -4;
    };
    copy_bytes(segment_id.as_bytes(), output, capacity)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn layout(
        id: &str,
        kind: StructuralObjectKind,
        rows: u32,
        columns: u32,
    ) -> SegmentObjectLayout {
        SegmentObjectLayout {
            object_id: id.to_owned(),
            object_kind: kind,
            logical_row_count: rows,
            logical_column_count: columns,
        }
    }

    fn segment(
        id: &str,
        object: &str,
        kind: StructuralObjectKind,
        row: u32,
        column: u32,
        text: &str,
    ) -> AssemblerSegment {
        AssemblerSegment {
            segment_id: id.to_owned(),
            object_id: object.to_owned(),
            object_kind: kind,
            row,
            column,
            row_span: 1,
            column_span: 1,
            text: text.to_owned(),
        }
    }

    #[test]
    fn trusted_pdf_preserves_sparse_cells_and_ratio_formatting() {
        let artifact = assemble_segment_topology(
            AssemblerSource::TrustedPdfTextLayer,
            &[layout("table", StructuralObjectKind::Table, 2, 3)],
            &[
                segment("a", "table", StructuralObjectKind::Table, 0, 0, "Metric"),
                segment("b", "table", StructuralObjectKind::Table, 0, 2, "Value"),
                segment("c", "table", StructuralObjectKind::Table, 1, 2, "7/8"),
            ],
        )
        .unwrap();
        assert_eq!(
            artifact.markdown,
            "| Metric |  | Value |\n| --- | --- | --- |\n|  |  | 7 / 8 |"
        );
        assert_eq!(artifact.objects.len(), 1);
        assert_eq!(artifact.objects[0].cells.len(), 3);
    }

    #[test]
    fn raster_attaches_an_exact_width_paragraph_header() {
        let artifact = assemble_segment_topology(
            AssemblerSource::RasterGeometry,
            &[
                layout("header", StructuralObjectKind::Paragraph, 1, 1),
                layout("table", StructuralObjectKind::Table, 1, 2),
            ],
            &[
                segment(
                    "h",
                    "header",
                    StructuralObjectKind::Paragraph,
                    0,
                    0,
                    "Name Value",
                ),
                segment("a", "table", StructuralObjectKind::Table, 0, 0, "alpha"),
                segment("b", "table", StructuralObjectKind::Table, 0, 1, "beta"),
            ],
        )
        .unwrap();
        assert_eq!(artifact.objects.len(), 1);
        assert_eq!(
            artifact.markdown,
            "| Name | Value |\n| --- | --- |\n| alpha | beta |"
        );
    }

    #[test]
    fn conflicting_inferred_span_yields_to_a_real_anchor() {
        let mut spanning = segment("wide", "table", StructuralObjectKind::Table, 0, 0, "left");
        spanning.column_span = 3;
        let artifact = assemble_segment_topology(
            AssemblerSource::TrustedPdfTextLayer,
            &[layout("table", StructuralObjectKind::Table, 1, 3)],
            &[
                spanning,
                segment(
                    "anchor",
                    "table",
                    StructuralObjectKind::Table,
                    0,
                    2,
                    "right",
                ),
            ],
        )
        .unwrap();
        assert_eq!(artifact.objects[0].cells[0].column_span, 2);
        assert_eq!(
            artifact.markdown,
            "| left |  | right |\n| --- | --- | --- |"
        );
    }
}
