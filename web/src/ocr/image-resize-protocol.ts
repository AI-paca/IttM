import type { BrowserLayoutPipelineConfig } from "./layout-contracts";

export interface ResizeWorkerRequest {
  file: File;
  maxImagePixels: number;
  maxDimension: number;
  ocrBorderPixels: number;
  spatialFullPageFallback: boolean;
  darkUiTextFallback: boolean;
  layout: BrowserLayoutPipelineConfig;
}

export type ResizeWorkerCommand =
  | { type: "start"; request: ResizeWorkerRequest }
  | { type: "next" };

export type ResizeWorkerResponse =
  | { type: "plan"; total: number }
  | { type: "passthrough"; total: 1; width: number; height: number }
  | {
      type: "tile";
      index: number;
      total: number;
      blob: Blob;
      width: number;
      height: number;
    }
  | { type: "complete"; total: number }
  | { type: "error"; error: string };
