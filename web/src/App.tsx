import { OcrProvider } from "./ocr/OcrContext";
import { AppShell } from "./ui/AppShell";

export default function App() {
  return (
    <OcrProvider>
      <AppShell />
    </OcrProvider>
  );
}
