import { FileText } from "lucide-react";

interface ConfigurePanelProps {
  onStartExtraction: () => void;
}

export function ConfigurePanel({ onStartExtraction }: ConfigurePanelProps) {
  return (
    <div className="flex-1 w-full flex flex-col justify-end animate-in fade-in duration-500 pb-4 sm:pb-8 mt-2 sm:mt-4">
      <div className="w-full">
        <button
          onClick={onStartExtraction}
          className="w-full py-4 sm:py-5 bg-blue-600 hover:bg-blue-700 text-white text-lg sm:text-xl font-bold rounded-2xl md:rounded-3xl shadow-xl shadow-blue-500/20 transition-all active:scale-[0.98] focus:outline-none focus:ring-4 focus:ring-blue-500/50 flex items-center justify-center gap-3 group"
        >
          <FileText className="w-7 h-7 sm:w-8 sm:h-8 outline-none bg-blue-500/50 p-1.5 rounded-lg group-hover:scale-110 transition-transform hidden sm:block backdrop-blur-sm" />
          Получить текст
        </button>
      </div>
    </div>
  );
}
