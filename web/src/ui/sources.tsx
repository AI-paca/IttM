import React from "react";
import { Cloud, Cpu, HardDrive, Sparkles, Wand2 } from "lucide-react";
import type { SourceType } from "../ocr/types";

export interface SourceOption {
  id: SourceType;
  label: string;
  desc: string;
  icon: React.ReactNode;
}

export const SOURCES: SourceOption[] = [
  {
    id: "auto",
    label: "Auto (Fallback)",
    desc: "Cloud -> Local -> Browser",
    icon: <Wand2 className="w-4 h-4" />,
  },
  {
    id: "gateway",
    label: "Gateway API",
    desc: "Nginx / custom gateway",
    icon: <Cloud className="w-4 h-4" />,
  },
  {
    id: "browser",
    label: "Browser Engine",
    desc: "WASM On-Device (No backend)",
    icon: <HardDrive className="w-4 h-4" />,
  },
  {
    id: "local_tess",
    label: "Local Tesseract",
    desc: "Python API (Базовый)",
    icon: <Cpu className="w-4 h-4" />,
  },
  {
    id: "local_easy",
    label: "Local EasyOCR",
    desc: "Python API (~5ГБ)",
    icon: <Cpu className="w-4 h-4" />,
  },
  {
    id: "llm",
    label: "LLM Cloud API",
    desc: "Gemini / OpenRouter",
    icon: <Sparkles className="w-4 h-4" />,
  },
];
