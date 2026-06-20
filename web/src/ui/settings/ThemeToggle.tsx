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
 *
 * Рефакторинг: gray-* классы заменены на .segmented-control и токены
 * (surface, text-secondary, accent) из ui/theme/components.css.
 */
export function ThemeToggle({ themeMode, onChange }: ThemeToggleProps) {
  return (
    <div className="segmented-control w-full">
      {MODES.map(({ id, label, icon: Icon }) => {
        const active = themeMode === id;
        return (
          <button
            key={id}
            onClick={() => onChange(id)}
            className={`segmented-btn ${active ? "segmented-btn-active" : ""}`}
          >
            {label ?? (Icon ? <Icon className="w-4 h-4" /> : null)}
          </button>
        );
      })}
    </div>
  );
}
