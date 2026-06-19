import { createBrowserOcrProfile } from "../web/src/ocr/browser-profile";
import { resolveBrowserBenchmarkProfile } from "./browser-benchmark-profile";

const profileName = process.argv[2] || process.env.BROWSER_OCR_PROFILE;
const pipelineProfile = resolveBrowserBenchmarkProfile(profileName);

process.stdout.write(
  JSON.stringify(createBrowserOcrProfile(null, pipelineProfile)),
);
