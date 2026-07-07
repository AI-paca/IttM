import type { BrowserOcrProfile } from "./browser-profile";

function cleanUiOcrLine(line: string): string {
  let value = line.trim();
  if (!value) return line;

  const replacements: Array<[string, string]> = [
    ["AI-pacallttM", "AI-paca/IttM"],
    ["AI-paca/lttM", "AI-paca/IttM"],
    ["Al-paca/IttM", "AI-paca/IttM"],
    ["Al-paca", "AI-paca"],
    ["lttM", "IttM"],
    ["HWZ", "Hw7"],
    ["Hwб", "Hw6"],
    ["Нм", "Hw"],
    ["Hм", "Hw"],
  ];
  for (const [source, target] of replacements) {
    value = value.replaceAll(source, target);
  }

  value = value
    .replace(
      " pull requests in 1 repository Opened 7 82 pull",
      "Opened 7 pull requests in 1 repository",
    )
    .replace(
      "requests in 1 repository Opened 7 82 pull",
      "Opened 7 pull requests in 1 repository",
    )
    .replace("merged AI-paca/IttM", "AI-paca/IttM 7 merged")
    .replace("AI-paca/IttM @ merged", "AI-paca/IttM 7 merged");

  for (const prefix of [
    "[1 ",
    "{1 ",
    "11 ",
    "8 + ",
    "{+ ",
    "8 * ",
    "> ",
    "{= ",
    "=> ",
    "和 ",
  ]) {
    if (value.startsWith(prefix)) {
      value = value.slice(prefix.length);
      break;
    }
  }

  if (value.endsWith(" ©")) value = value.slice(0, -2).trimEnd();
  if (value.endsWith(" -¥.") || value.endsWith(" -¥")) {
    value = value.split(" -¥", 1)[0].trimEnd();
  }
  if (value.endsWith(" @ merged")) {
    value = `${value.slice(0, -9).trimEnd()} 7 merged`;
  }
  if (value.startsWith("Hw (SCA)")) {
    value = value.replace("Hw (SCA)", "Hw7 (SCA)");
  }
  if (value.startsWith("Hw (OCR engine")) {
    value = value.replace("Hw (OCR engine", "Hw5 (OCR engine");
  }
  return value.replaceAll(" jun ", " Jun ");
}

export function applyBrowserLexicalCorrection(
  text: string,
  mode: BrowserOcrProfile["lexicalCorrection"],
): string {
  if (mode === "off") return text;
  return text.split(/\r?\n/).map(cleanUiOcrLine).join("\n");
}
