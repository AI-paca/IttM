import { Moon, Sun } from "lucide-react";
import type { ThemeMode } from "../../types/app.types";

interface ThemeToggleProps {
  themeMode: ThemeMode;
  onChange: (mode: ThemeMode) => void;
}

const MODES: { id: ThemeMode; label?: string; icon?: typeof Sun }[] = [
  { id: "auto", label: "Default" },
  { id: "light", icon: Sun },
  { id: "dark", icon: Moon },
];

/**
 * Segmented control переключателя темы: Default / Light / Dark.
 */
export function ThemeToggle({ themeMode, onChange }: ThemeToggleProps) {
  return (
    <div className="flex bg-gray-100 dark:bg-gray-800 p-1 rounded-xl w-full">
      {MODES.map(({ id, label, icon: Icon }) => {
        const active = themeMode === id;
        return (
          <button
            key={id}
            onClick={() => onChange(id)}
            className={`flex-1 flex justify-center items-center py-1.5 text-xs font-bold rounded-lg transition-all ${
              active
                ? "bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white"
                : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"
            }`}
          >
            {label ?? (Icon ? <Icon className="w-4 h-4" /> : null)}
          </button>
        );
      })}
    </div>
  );
}
