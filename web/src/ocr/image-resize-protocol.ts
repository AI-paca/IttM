import type { BrowserLayoutPipelineConfig } from "./layout-contracts";

export interface ResizeWorkerRequest {
  file: File;
  maxImagePixels: number;
  maxDimension: number;
  layout: BrowserLayoutPipelineConfig;
}

export type ResizeWorkerCommand =
  | { type: "start"; request: ResizeWorkerRequest }
  | { type: "next" };

export type ResizeWorkerResponse =
  | { type: "plan"; total: number }
  | { type: "passthrough"; total: 1 }
  | { type: "tile"; index: number; total: number; blob: Blob }
  | { type: "complete"; total: number }
  | { type: "error"; error: string };
