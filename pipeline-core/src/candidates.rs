#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SpanEvidence {
    pub ocr_confidence_milli: u32,
    pub script_consistency_milli: u32,
    pub context_consistency_milli: u32,
    pub source_agreement: u32,
    pub contradictions: u32,
}

const EVIDENCE_LIMIT: u32 = 1_000;
const AGREEMENT_LIMIT: u32 = 4;
const CONTRADICTION_LIMIT: u32 = 4;

const fn bounded(value: u32, limit: u32) -> i64 {
    if value > limit {
        limit as i64
    } else {
        value as i64
    }
}

/// Score evidence for an already observed OCR span.
///
/// The core deliberately receives no text, language name, fixture identity or
/// mutable prior.  It can rank evidence but cannot synthesize a replacement.
pub const fn score_span_evidence(evidence: SpanEvidence) -> i32 {
    let confidence = bounded(evidence.ocr_confidence_milli, EVIDENCE_LIMIT);
    let script = bounded(evidence.script_consistency_milli, EVIDENCE_LIMIT);
    let context = bounded(evidence.context_consistency_milli, EVIDENCE_LIMIT);
    let agreement = bounded(evidence.source_agreement, AGREEMENT_LIMIT);
    let contradictions = bounded(evidence.contradictions, CONTRADICTION_LIMIT);

    (confidence * 35 + script * 20 + context * 15 + agreement * 7_500 - contradictions * 15_000)
        as i32
}

#[cfg(test)]
mod tests {
    use super::*;

    fn evidence() -> SpanEvidence {
        SpanEvidence {
            ocr_confidence_milli: 800,
            script_consistency_milli: 900,
            context_consistency_milli: 700,
            source_agreement: 2,
            contradictions: 0,
        }
    }

    #[test]
    fn score_is_monotonic_for_positive_evidence() {
        let baseline = score_span_evidence(evidence());
        assert!(
            score_span_evidence(SpanEvidence {
                ocr_confidence_milli: 900,
                ..evidence()
            }) > baseline
        );
        assert!(
            score_span_evidence(SpanEvidence {
                source_agreement: 3,
                ..evidence()
            }) > baseline
        );
    }

    #[test]
    fn contradictions_have_a_strict_penalty() {
        assert!(
            score_span_evidence(SpanEvidence {
                contradictions: 1,
                ..evidence()
            }) < score_span_evidence(evidence())
        );
    }

    #[test]
    fn untrusted_counters_are_bounded() {
        assert_eq!(
            score_span_evidence(SpanEvidence {
                ocr_confidence_milli: u32::MAX,
                script_consistency_milli: u32::MAX,
                context_consistency_milli: u32::MAX,
                source_agreement: u32::MAX,
                contradictions: u32::MAX,
            }),
            40_000
        );
    }
}
