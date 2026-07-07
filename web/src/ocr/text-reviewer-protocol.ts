export interface BrowserTextCandidate {
  text: string;
  left?: string;
  top?: string;
  confidence?: number | null;
}

export interface TextReviewerRequest {
  id: number;
  type: "review";
  candidate: BrowserTextCandidate;
}

export interface TextReviewerResponse {
  id: number;
  type: "result" | "error";
  isText?: boolean;
  error?: string;
}
