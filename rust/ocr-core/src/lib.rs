#[cfg(feature = "wasm")]
use wasm_bindgen::prelude::*;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GrammarAssessment {
    pub percent: u8,
    pub exact: bool,
    pub reasons: Vec<&'static str>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Script {
    Neutral,
    Cyrillic,
    Latin,
    Greek,
    Cjk,
    Hiragana,
    Katakana,
    Other,
}

#[derive(Clone, Copy, Debug, Default)]
struct AllowedScripts {
    cyrillic: bool,
    latin: bool,
    greek: bool,
    cjk: bool,
    hiragana: bool,
    katakana: bool,
}

impl AllowedScripts {
    fn from_languages(languages: &str) -> Self {
        let mut allowed = Self::default();
        for language in languages
            .split(['+', ',', ' '])
            .filter(|value| !value.is_empty())
        {
            match language {
                "rus" | "Cyrillic" => allowed.cyrillic = true,
                "eng" | "Latin" => allowed.latin = true,
                "ell" | "Greek" => allowed.greek = true,
                "chi_sim" | "chi_tra" | "HanS" | "HanT" => {
                    allowed.cjk = true;
                    allowed.hiragana = true;
                    allowed.katakana = true;
                }
                "equ" => {
                    allowed.latin = true;
                    allowed.greek = true;
                }
                _ => {}
            }
        }
        allowed
    }

    fn contains(self, script: Script) -> bool {
        match script {
            Script::Neutral => true,
            Script::Cyrillic => self.cyrillic,
            Script::Latin => self.latin,
            Script::Greek => self.greek,
            Script::Cjk => self.cjk,
            Script::Hiragana => self.hiragana,
            Script::Katakana => self.katakana,
            Script::Other => false,
        }
    }
}

fn character_script(character: char) -> Script {
    if !character.is_alphabetic() {
        return Script::Neutral;
    }
    match character as u32 {
        0x0041..=0x024f | 0x1e00..=0x1eff => Script::Latin,
        0x0370..=0x03ff | 0x1f00..=0x1fff => Script::Greek,
        0x0400..=0x052f | 0x2de0..=0x2dff | 0xa640..=0xa69f => {
            Script::Cyrillic
        }
        0x3040..=0x309f => Script::Hiragana,
        0x30a0..=0x30ff | 0x31f0..=0x31ff => Script::Katakana,
        0x3400..=0x4dbf | 0x4e00..=0x9fff | 0xf900..=0xfaff => {
            Script::Cjk
        }
        _ => Script::Other,
    }
}

fn is_vowel(character: char) -> bool {
    matches!(
        character,
        'a' | 'e' | 'i' | 'o' | 'u' | 'y'
            | 'A' | 'E' | 'I' | 'O' | 'U' | 'Y'
            | 'а' | 'е' | 'ё' | 'и' | 'о' | 'у' | 'ы' | 'э' | 'ю' | 'я'
            | 'А' | 'Е' | 'Ё' | 'И' | 'О' | 'У' | 'Ы' | 'Э' | 'Ю' | 'Я'
            | 'α' | 'ε' | 'η' | 'ι' | 'ο' | 'υ' | 'ω'
            | 'Α' | 'Ε' | 'Η' | 'Ι' | 'Ο' | 'Υ' | 'Ω'
    )
}

fn normalized_confidence(value: f64) -> f64 {
    if !value.is_finite() {
        return 0.0;
    }
    if value > 1.0 {
        (value / 100.0).clamp(0.0, 1.0)
    } else {
        value.clamp(0.0, 1.0)
    }
}

fn is_supported_symbol(character: char) -> bool {
    ".,:;!?%+-=*/()[]{}<>_|#@'\"".contains(character)
}

fn has_suspicious_script_mix(tokens: &[&str]) -> bool {
    fn index(script: Script) -> Option<usize> {
        match script {
            Script::Cyrillic => Some(0),
            Script::Latin => Some(1),
            Script::Greek => Some(2),
            Script::Cjk => Some(3),
            Script::Hiragana => Some(4),
            Script::Katakana => Some(5),
            Script::Neutral | Script::Other => None,
        }
    }

    let mut counts = [0_usize; 6];
    for token in tokens {
        let mut token_script: Option<Script> = None;
        for script in token
            .chars()
            .filter_map(|character| index(character_script(character)).map(|_| character_script(character)))
        {
            if token_script.is_some_and(|existing| existing != script) {
                return true;
            }
            token_script = Some(script);
            counts[index(script).expect("a retained script has an index")] += 1;
        }
    }
    let Some((dominant_index, dominant_count)) =
        counts.iter().copied().enumerate().max_by_key(|(_, count)| *count)
    else {
        return false;
    };
    let minority_count = counts.iter().sum::<usize>() - dominant_count;
    if minority_count == 0 || dominant_count < minority_count * 3 {
        return false;
    }
    tokens.iter().any(|token| {
        let token_scripts: Vec<usize> = token
            .chars()
            .filter_map(|character| index(character_script(character)))
            .collect();
        !token_scripts.is_empty()
            && token_scripts.len() <= 3
            && token_scripts.iter().all(|value| *value != dominant_index)
    })
}

pub fn assess_grammar(
    text: &str,
    languages: &str,
    word_confidences: &[f64],
) -> GrammarAssessment {
    let text = text.trim();
    if text.is_empty() {
        return GrammarAssessment {
            percent: 0,
            exact: false,
            reasons: vec!["empty"],
        };
    }

    let confidences: Vec<f64> = word_confidences
        .iter()
        .copied()
        .map(normalized_confidence)
        .collect();
    let mean_confidence = if confidences.is_empty() {
        0.0
    } else {
        confidences.iter().sum::<f64>() / confidences.len() as f64
    };
    let minimum_confidence = confidences.iter().copied().reduce(f64::min).unwrap_or(0.0);

    let letters: Vec<char> = text.chars().filter(|character| character.is_alphabetic()).collect();
    let allowed = AllowedScripts::from_languages(languages);
    let matching_letters = letters
        .iter()
        .filter(|character| allowed.contains(character_script(**character)))
        .count();
    let script_fit = if letters.is_empty() {
        1.0
    } else {
        matching_letters as f64 / letters.len() as f64
    };

    let visible: Vec<char> = text.chars().filter(|character| !character.is_whitespace()).collect();
    let symbols = visible
        .iter()
        .filter(|character| {
            !character.is_alphanumeric() && !is_supported_symbol(**character)
        })
        .count();
    let symbol_ratio = if visible.is_empty() {
        1.0
    } else {
        symbols as f64 / visible.len() as f64
    };
    let controls = text
        .chars()
        .filter(|character| character.is_control() && !character.is_whitespace())
        .count();

    let tokens: Vec<&str> = text
        .split(|character: char| !character.is_alphanumeric() && character != '_')
        .filter(|token| !token.is_empty())
        .collect();
    let lexical: Vec<&str> = tokens
        .iter()
        .copied()
        .filter(|token| token.chars().any(|character| character.is_alphabetic()))
        .collect();
    let implausible = lexical
        .iter()
        .filter(|token| {
            let token_letters: Vec<char> = token
                .chars()
                .filter(|character| character.is_alphabetic())
                .collect();
            token_letters.len() >= 4
                && !token_letters.iter().all(|character| character.is_uppercase())
                && !token_letters.iter().copied().any(is_vowel)
        })
        .count();
    let lexical_fit = if lexical.is_empty() {
        if text.chars().any(|character| character.is_numeric()) {
            1.0
        } else {
            0.0
        }
    } else {
        1.0 - implausible as f64 / lexical.len() as f64
    };
    let singletons = lexical
        .iter()
        .filter(|token| token.chars().count() == 1)
        .count();
    let singleton_ratio = if lexical.is_empty() {
        0.0
    } else {
        singletons as f64 / lexical.len() as f64
    };
    let semantic_shape = !letters.is_empty()
        || tokens.len() > 1
        || text.chars().any(|character| "%=+-*/".contains(character));
    let suspicious_script_mix = has_suspicious_script_mix(&tokens);
    let nonempty_line_token_counts: Vec<usize> = text
        .lines()
        .map(|line| {
            line.split(|character: char| {
                !character.is_alphanumeric() && character != '_'
            })
            .filter(|token| !token.is_empty())
            .count()
        })
        .filter(|count| *count > 0)
        .collect();
    let fragmented_lines = nonempty_line_token_counts.len() >= 6
        && nonempty_line_token_counts
            .iter()
            .filter(|count| **count == 1)
            .count()
            * 2
            >= nonempty_line_token_counts.len();

    let mut score = mean_confidence * 0.48
        + script_fit * 0.28
        + lexical_fit * 0.18
        + (1.0 - (symbol_ratio * 3.0).min(1.0)) * 0.06;
    score -= (singleton_ratio * 0.25).min(0.35);
    score -= (controls as f64 * 0.20).min(0.50);
    if fragmented_lines {
        score -= 0.10;
    }
    if suspicious_script_mix {
        score -= 0.10;
    }
    let mut percent = (score * 100.0).round().clamp(0.0, 99.0) as u8;
    let exact = !confidences.is_empty()
        && mean_confidence >= 0.92
        && minimum_confidence >= 0.75
        && script_fit == 1.0
        && lexical_fit == 1.0
        && singleton_ratio <= 0.20
        && symbol_ratio <= 0.05
        && controls == 0
        && semantic_shape
        && !fragmented_lines
        && !suspicious_script_mix;

    let mut reasons = Vec::new();
    if mean_confidence < 0.92 {
        reasons.push("confidence");
    }
    if script_fit < 1.0 {
        reasons.push("script");
    }
    if lexical_fit < 1.0 {
        reasons.push("t9-shape");
    }
    if singleton_ratio > 0.20 {
        reasons.push("singletons");
    }
    if symbol_ratio > 0.05 || controls > 0 {
        reasons.push("garbage-symbols");
    }
    if !semantic_shape {
        reasons.push("context-shape");
    }
    if suspicious_script_mix {
        reasons.push("mixed-script-substitution");
    }
    if fragmented_lines {
        reasons.push("fragmented-lines");
    }
    if exact {
        percent = 100;
        reasons.push("exact");
    }
    GrammarAssessment {
        percent,
        exact,
        reasons,
    }
}

#[cfg(feature = "wasm")]
#[wasm_bindgen]
pub fn assess_grammar_json(
    text: &str,
    languages: &str,
    word_confidences: Vec<f64>,
) -> String {
    let assessment = assess_grammar(text, languages, &word_confidences);
    let reasons = assessment
        .reasons
        .iter()
        .map(|reason| format!("\"{reason}\""))
        .collect::<Vec<_>>()
        .join(",");
    format!(
        "{{\"percent\":{},\"exact\":{},\"reasons\":[{}]}}",
        assessment.percent, assessment.exact, reasons
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_clean_bilingual_evidence() {
        let assessment = assess_grammar(
            "README готов пройти проверку",
            "rus+eng",
            &[98.0, 97.0, 96.0, 99.0],
        );
        assert_eq!(assessment.percent, 100);
        assert!(assessment.exact);
    }

    #[test]
    fn rejects_a_script_outside_the_selected_engine() {
        let assessment = assess_grammar("刀 用 才", "rus+eng", &[96.0, 96.0, 96.0]);
        assert!(!assessment.exact);
        assert!(assessment.reasons.contains(&"script"));
    }

    #[test]
    fn rejects_implausible_word_shapes() {
        let assessment = assess_grammar("b4026691 zzzzz", "rus+eng", &[98.0, 98.0]);
        assert!(!assessment.exact);
        assert!(assessment.reasons.contains(&"t9-shape"));
    }

    #[test]
    fn rejects_fragmented_single_token_lines() {
        let assessment = assess_grammar(
            "Header\n\nValue\n\nConcept\n\nPercent\n\nEnglish\n\nTotal",
            "rus+eng",
            &[98.0; 6],
        );
        assert!(!assessment.exact);
        assert!(assessment.reasons.contains(&"fragmented-lines"));
    }

    #[test]
    fn rejects_a_short_minority_script_island() {
        let assessment = assess_grammar(
            "You Ме Concept English metrics",
            "rus+eng",
            &[98.0; 5],
        );
        assert!(!assessment.exact);
        assert!(
            assessment
                .reasons
                .contains(&"mixed-script-substitution")
        );
    }
}
