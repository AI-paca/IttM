interface RememberToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
}

/**
 * Чекбокс "Запомнить выбор (Cookies)" с кастомным стилем.
 *
 * Рефакторинг: gray/blue классы заменены на .checkbox-card из components.css.
 */
export function RememberToggle({ checked, onChange }: RememberToggleProps) {
  return (
    <div className="flex flex-col gap-3 px-1">
      <label className="relative flex items-center gap-3 cursor-pointer group">
        <input
          type="checkbox"
          className="peer sr-only"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
        />
        <div className="checkbox-indicator">
          <svg
            className="w-3.5 h-3.5 text-on-accent opacity-0 peer-checked:opacity-100 transition-opacity"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={3}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M5 13l4 4L19 7"
            />
          </svg>
        </div>
        <span className="checkbox-label">Запомнить выбор (Cookies)</span>
      </label>
    </div>
  );
}
