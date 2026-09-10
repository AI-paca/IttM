import type { BrowserOcrProfile } from "./browser-profile";

function cleanCommerceOcrConfusables(
  line: string,
  preferEuro: boolean,
): string {
  let value = line;
  if (preferEuro) {
    value = value.replace(/£(?=\s*\d)/g, "€");
  }
  value = splitCompactResolutionNumbers(value);
  return value
    .replace(/\bFHO\b/g, "FHD")
    .replace(/\bDRS\b/g, "DDR5")
    .replace(/\bRAM\s+868\b/g, "RAM 8GB")
    .replace(/\b868\s+RAM\b/g, "8GB RAM");
}

function splitCompactResolutionNumbers(line: string): string {
  if (
    !/\b(?:display|screen|monitor|resolution|pixel|pixels|ips|oled|lcd|retina|inch|laptop|notebook|fhd)\b/i.test(
      line,
    )
  ) {
    return line;
  }
  return line.replace(
    /(?<![\w])(\d{3,4})(\d{3,4})(?![\w])/g,
    (match, widthText: string, heightText: string) => {
      const width = Number(widthText);
      const height = Number(heightText);
      if (width >= 640 && width <= 9999 && height >= 480 && height <= 9999) {
        return `${width}x${height}`;
      }
      return match;
    },
  );
}

function cleanUiOcrLine(line: string, preferEuro: boolean): string {
  let value = line.trim();
  if (!value) return line;

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
  value = cleanCommerceOcrConfusables(value, preferEuro);
  return value
    .replaceAll(" jun ", " Jun ")
    .replace(/([\u3400-\u9fff])\s+(?=[\u3400-\u9fff])/gu, "$1");
}

export function applyBrowserLexicalCorrection(
  text: string,
  mode: BrowserOcrProfile["lexicalCorrection"],
): string {
  if (mode === "off") return text;
  const preferEuro = /€/.test(text);
  return text
    .split(/\r?\n/)
    .map((line) => cleanUiOcrLine(line, preferEuro))
    .join("\n");
}
