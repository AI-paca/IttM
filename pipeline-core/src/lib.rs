mod candidates;
mod separated;

pub use candidates::{SpanEvidence, score_span_evidence};

pub const ABI_VERSION: u32 = 5;

pub const MERGE_UP_CODE: u8 = 3;
pub const MERGE_LEFT_CODE: u8 = 5;
pub const EMPTY_SLOT_CODE: u8 = 11;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum PixelFormat {
    Gray8 = 1,
    Rgb8 = 3,
    Rgba8 = 4,
}

impl PixelFormat {
    pub const fn channels(self) -> u32 {
        self as u32
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ImagePlane {
    pub width: u32,
    pub height: u32,
    pub stride: u32,
    pub format: PixelFormat,
    pub pixels: Vec<u8>,
}

impl ImagePlane {
    pub fn is_valid(&self) -> bool {
        let minimum_stride = self.width.saturating_mul(self.format.channels());
        self.width > 0
            && self.height > 0
            && self.stride >= minimum_stride
            && self.pixels.len() == self.stride.saturating_mul(self.height) as usize
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum Stage {
    Align = 0,
    Segment = 1,
    ProjectSparse = 2,
    RecognizeSegments = 3,
    SelectLanguageCandidate = 4,
    LexicalCorrection = 5,
    GroupStructures = 6,
    RenderMarkdown = 7,
}

impl Stage {
    pub const fn bit(self) -> u32 {
        1_u32 << self as u8
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PipelineCapabilities {
    pub trusted_text: bool,
    pub provides_layout: bool,
    pub provides_markdown: bool,
    pub needs_language_retry: bool,
}

impl Default for PipelineCapabilities {
    fn default() -> Self {
        Self {
            trusted_text: false,
            provides_layout: false,
            provides_markdown: false,
            needs_language_retry: true,
        }
    }
}

impl PipelineCapabilities {
    pub const TRUSTED_TEXT: u32 = 1 << 0;
    pub const PROVIDES_LAYOUT: u32 = 1 << 1;
    pub const PROVIDES_MARKDOWN: u32 = 1 << 2;
    pub const NEEDS_LANGUAGE_RETRY: u32 = 1 << 3;

    pub const fn from_bits(bits: u32) -> Self {
        Self {
            trusted_text: bits & Self::TRUSTED_TEXT != 0,
            provides_layout: bits & Self::PROVIDES_LAYOUT != 0,
            provides_markdown: bits & Self::PROVIDES_MARKDOWN != 0,
            needs_language_retry: bits & Self::NEEDS_LANGUAGE_RETRY != 0,
        }
    }
}

pub const fn recipe_mask(capabilities: PipelineCapabilities) -> u32 {
    let mut stages = Stage::RecognizeSegments.bit();
    if !capabilities.provides_layout {
        stages |= Stage::Align.bit() | Stage::Segment.bit() | Stage::ProjectSparse.bit();
    }
    if capabilities.needs_language_retry && !capabilities.trusted_text {
        stages |= Stage::SelectLanguageCandidate.bit();
    }
    if !capabilities.trusted_text {
        stages |= Stage::LexicalCorrection.bit();
    }
    if !capabilities.provides_markdown {
        stages |= Stage::GroupStructures.bit() | Stage::RenderMarkdown.bit();
    }
    stages
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SparseCodeError {
    UnknownCode(u8),
    UnknownSignal(u8),
}

const fn signal_mask(signal: u8) -> Option<u8> {
    match signal {
        MERGE_UP_CODE => Some(1 << 0),
        MERGE_LEFT_CODE => Some(1 << 1),
        EMPTY_SLOT_CODE => Some(1 << 2),
        _ => None,
    }
}

const fn component_mask(code: u8) -> Option<u8> {
    match code {
        0 => Some(0),
        3 => Some(1 << 0),
        5 => Some(1 << 1),
        8 => Some((1 << 0) | (1 << 1)),
        11 => Some(1 << 2),
        14 => Some((1 << 0) | (1 << 2)),
        16 => Some((1 << 1) | (1 << 2)),
        19 => Some((1 << 0) | (1 << 1) | (1 << 2)),
        _ => None,
    }
}

const fn code_from_mask(mask: u8) -> u8 {
    let mut code = 0;
    if mask & (1 << 0) != 0 {
        code += MERGE_UP_CODE;
    }
    if mask & (1 << 1) != 0 {
        code += MERGE_LEFT_CODE;
    }
    if mask & (1 << 2) != 0 {
        code += EMPTY_SLOT_CODE;
    }
    code
}

pub const fn add_sparse_signal(code: u8, signal: u8) -> Result<u8, SparseCodeError> {
    let Some(signal_component) = signal_mask(signal) else {
        return Err(SparseCodeError::UnknownSignal(signal));
    };
    let Some(components) = component_mask(code) else {
        return Err(SparseCodeError::UnknownCode(code));
    };
    Ok(code_from_mask(components | signal_component))
}

pub const fn is_isolated_heading(
    run_rows: u32,
    content_chars: u32,
    max_line_chars: u32,
    bounded_above: bool,
    bounded_below: bool,
) -> bool {
    run_rows > 0
        && run_rows <= 2
        && content_chars >= 6
        && max_line_chars < 90
        && bounded_above
        && bounded_below
}

pub const fn should_replace_primary(
    primary_chars: u32,
    fallback_chars: u32,
    primary_tokens: u32,
    retained_primary_tokens: u32,
) -> bool {
    if primary_chars < 10 {
        if primary_chars > 0
            && primary_chars <= 3
            && primary_tokens <= 1
            && retained_primary_tokens == 0
            && fallback_chars >= 6
            && (fallback_chars as u64) >= (primary_chars as u64) * 3
        {
            return true;
        }
        return fallback_chars >= 80;
    }

    if primary_tokens == 0 {
        return false;
    }

    // Work in u64 so hostile or corrupted C/WASM inputs cannot overflow the
    // 1.4x growth calculation. Adding 9 implements integer ceil(/ 10).
    let scaled_primary = ((primary_chars as u64) * 14 + 9) / 10;
    let minimum_fallback = if scaled_primary > 180 {
        scaled_primary
    } else {
        180
    };
    if (fallback_chars as u64) < minimum_fallback {
        return false;
    }

    let retained = if retained_primary_tokens > primary_tokens {
        primary_tokens
    } else {
        retained_primary_tokens
    };
    (retained as u64) * 5 >= (primary_tokens as u64) * 4
}

pub const fn should_drop_text_block(
    candidate_chars: u32,
    existing_chars: u32,
    shared_tokens: u32,
    candidate_tokens: u32,
    existing_tokens: u32,
    similarity_milli: u32,
) -> bool {
    if candidate_chars < 32 || existing_chars < 32 {
        return false;
    }

    let similarity = if similarity_milli > 1_000 {
        1_000
    } else {
        similarity_milli
    };
    if similarity >= 880 {
        return true;
    }

    if candidate_tokens == 0 || existing_tokens == 0 {
        return false;
    }

    // A shared-token count cannot exceed either block. Clamp it before
    // calculating coverage so invalid FFI inputs never manufacture overlap.
    // The decision is intentionally directional: the incoming candidate may
    // be a repeated heading/subset of an existing full segment.
    let smaller_block = if candidate_tokens < existing_tokens {
        candidate_tokens
    } else {
        existing_tokens
    };
    let shared = if shared_tokens > smaller_block {
        smaller_block
    } else {
        shared_tokens
    };
    (shared as u64) * 1_000 >= (candidate_tokens as u64) * 850
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BoxI32 {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SparseCode {
    pub row: u32,
    pub column: u32,
    pub code: u8,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Segment {
    pub id: u32,
    pub source_bbox: BoxI32,
    pub content_bbox: BoxI32,
    pub anchor: Option<(u32, u32)>,
    pub codes: Vec<SparseCode>,
    pub structural_only: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SparseProjection {
    pub rows: u32,
    pub columns: u32,
    pub x_tracks: Vec<i32>,
    pub codes: Vec<SparseCode>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RecognizedSegment {
    pub kind: String,
    pub bbox: BoxI32,
    pub parts: Vec<String>,
    pub anchor: Option<(u32, u32)>,
    pub codes: Vec<SparseCode>,
    pub list_marker: bool,
    pub content_left: Option<i32>,
    pub counters: Vec<(String, u32)>,
    pub flags: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PipelineStageTrace {
    pub version: u32,
    pub stage: Stage,
    pub input_count: u32,
    pub output_count: u32,
    pub flags: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StructuralRecord {
    pub kind: String,
    pub bbox: BoxI32,
    pub parts: Vec<String>,
    pub anchor: Option<(u32, u32)>,
    pub codes: Vec<SparseCode>,
    pub list_marker: bool,
    pub content_left: Option<i32>,
    pub flags: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StructuralRenderArtifact {
    pub parts: Vec<String>,
    pub flags: Vec<String>,
    pub bypassed: bool,
    pub lossy_merge_rejected: bool,
    pub lint_pass: Option<bool>,
    pub confirmed: bool,
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_pipeline_abi_version() -> u32 {
    ABI_VERSION
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_pipeline_recipe_mask(capability_bits: u32) -> u32 {
    recipe_mask(PipelineCapabilities::from_bits(capability_bits))
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_sparse_add_signal(code: u32, signal: u32) -> i32 {
    let Ok(code) = u8::try_from(code) else {
        return -1;
    };
    let Ok(signal) = u8::try_from(signal) else {
        return -2;
    };
    match add_sparse_signal(code, signal) {
        Ok(value) => i32::from(value),
        Err(SparseCodeError::UnknownCode(_)) => -1,
        Err(SparseCodeError::UnknownSignal(_)) => -2,
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_is_isolated_heading(
    run_rows: u32,
    content_chars: u32,
    max_line_chars: u32,
    boundary_bits: u32,
) -> u32 {
    u32::from(is_isolated_heading(
        run_rows,
        content_chars,
        max_line_chars,
        boundary_bits & 1 != 0,
        boundary_bits & 2 != 0,
    ))
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_should_replace_primary(
    primary_chars: u32,
    fallback_chars: u32,
    primary_tokens: u32,
    retained_primary_tokens: u32,
) -> u32 {
    u32::from(should_replace_primary(
        primary_chars,
        fallback_chars,
        primary_tokens,
        retained_primary_tokens,
    ))
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_should_drop_text_block(
    candidate_chars: u32,
    existing_chars: u32,
    shared_tokens: u32,
    candidate_tokens: u32,
    existing_tokens: u32,
    similarity_milli: u32,
) -> u32 {
    u32::from(should_drop_text_block(
        candidate_chars,
        existing_chars,
        shared_tokens,
        candidate_tokens,
        existing_tokens,
        similarity_milli,
    ))
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_span_evidence_score(
    ocr_confidence_milli: u32,
    script_consistency_milli: u32,
    context_consistency_milli: u32,
    source_agreement: u32,
    contradictions: u32,
) -> i32 {
    score_span_evidence(SpanEvidence {
        ocr_confidence_milli,
        script_consistency_milli,
        context_consistency_milli,
        source_agreement,
        contradictions,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sparse_codes_match_the_pipeline_contract() {
        assert_eq!(add_sparse_signal(0, MERGE_UP_CODE), Ok(3));
        assert_eq!(add_sparse_signal(3, MERGE_LEFT_CODE), Ok(8));
        assert_eq!(add_sparse_signal(3, EMPTY_SLOT_CODE), Ok(14));
        assert_eq!(add_sparse_signal(5, EMPTY_SLOT_CODE), Ok(16));
        assert_eq!(add_sparse_signal(8, EMPTY_SLOT_CODE), Ok(19));
        assert_eq!(add_sparse_signal(14, MERGE_UP_CODE), Ok(14));
    }

    #[test]
    fn trusted_markdown_api_runs_only_recognition() {
        let capabilities = PipelineCapabilities {
            trusted_text: true,
            provides_layout: true,
            provides_markdown: true,
            needs_language_retry: false,
        };
        assert_eq!(recipe_mask(capabilities), Stage::RecognizeSegments.bit());
    }

    #[test]
    fn local_ocr_runs_the_complete_recipe() {
        assert_eq!(recipe_mask(PipelineCapabilities::default()), u8::MAX.into());
    }

    #[test]
    fn sparse_errors_match_python_validation_order() {
        assert_eq!(
            add_sparse_signal(1, 1),
            Err(SparseCodeError::UnknownSignal(1))
        );
        assert_eq!(
            add_sparse_signal(1, MERGE_UP_CODE),
            Err(SparseCodeError::UnknownCode(1))
        );
    }

    #[test]
    fn image_plane_validates_stride_and_payload() {
        let image = ImagePlane {
            width: 2,
            height: 2,
            stride: 6,
            format: PixelFormat::Rgb8,
            pixels: vec![255; 12],
        };
        assert!(image.is_valid());

        let truncated = ImagePlane {
            pixels: vec![255; 11],
            ..image
        };
        assert!(!truncated.is_valid());
    }

    #[test]
    fn isolated_heading_requires_short_content_and_both_boundaries() {
        assert!(is_isolated_heading(2, 18, 12, true, true));
        assert!(!is_isolated_heading(1, 2, 2, true, true));
        assert!(!is_isolated_heading(3, 18, 8, true, true));
        assert!(!is_isolated_heading(1, 18, 8, true, false));
    }

    #[test]
    fn primary_replacement_allows_only_substantial_fallback_for_tiny_input() {
        assert!(!should_replace_primary(0, 79, 0, 0));
        assert!(should_replace_primary(0, 80, 0, 0));
        assert!(should_replace_primary(9, 80, 0, u32::MAX));
        assert!(should_replace_primary(1, 6, 1, 0));
        assert!(!should_replace_primary(1, 3, 1, 0));
        assert!(!should_replace_primary(3, 9, 1, 1));
    }

    #[test]
    fn primary_replacement_requires_growth_and_token_retention() {
        assert!(!should_replace_primary(100, 179, 5, 5));
        assert!(should_replace_primary(100, 180, 5, 4));
        assert!(!should_replace_primary(100, 180, 5, 3));
        assert!(!should_replace_primary(200, 279, 5, 5));
        assert!(should_replace_primary(200, 280, 5, 5));
        assert!(!should_replace_primary(129, 180, 5, 5));
        assert!(should_replace_primary(129, 181, 5, 5));
    }

    #[test]
    fn primary_replacement_rejects_missing_tokens_and_handles_extreme_counts() {
        assert!(!should_replace_primary(10, 180, 0, 0));
        assert!(should_replace_primary(10, 180, 5, u32::MAX));
        assert!(!should_replace_primary(
            u32::MAX,
            u32::MAX,
            u32::MAX,
            u32::MAX,
        ));
    }

    #[test]
    fn duplicate_drop_requires_two_substantial_blocks() {
        assert!(!should_drop_text_block(31, 100, 10, 10, 10, 1_000));
        assert!(!should_drop_text_block(100, 31, 10, 10, 10, 1_000));
        assert!(should_drop_text_block(32, 32, 0, 0, 0, 880));
    }

    #[test]
    fn duplicate_drop_accepts_similarity_or_incoming_token_coverage() {
        assert!(!should_drop_text_block(100, 100, 0, 0, 0, 879));
        assert!(should_drop_text_block(100, 100, 0, 0, 0, 880));
        assert!(should_drop_text_block(100, 100, 17, 20, 20, 0));
        assert!(!should_drop_text_block(100, 100, 16, 20, 20, 0));
        assert!(should_drop_text_block(100, 100, 17, 20, 21, 0));
        assert!(!should_drop_text_block(100, 100, 17, 21, 20, 0));
    }

    #[test]
    fn duplicate_drop_clamps_untrusted_ffi_metrics() {
        assert!(should_drop_text_block(100, 100, 0, 0, 0, u32::MAX));
        assert!(should_drop_text_block(100, 100, u32::MAX, 10, 12, 0));
        assert!(should_drop_text_block(100, 100, u32::MAX, 10, 10, 0));
    }

    #[test]
    fn abi_exposes_the_new_deterministic_decisions() {
        assert_eq!(ittm_pipeline_abi_version(), 5);
        assert_eq!(ittm_should_replace_primary(100, 180, 5, 4), 1);
        assert_eq!(ittm_should_replace_primary(100, 180, 5, 3), 0);
        assert_eq!(ittm_should_drop_text_block(32, 32, 17, 20, 20, 0), 1);
        assert_eq!(ittm_should_drop_text_block(32, 32, 16, 20, 20, 0), 0);
    }
}
