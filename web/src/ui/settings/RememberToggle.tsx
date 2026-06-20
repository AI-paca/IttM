interface RememberToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
}

/**
 * Чекбокс "Запомнить выбор (Cookies)" с кастомным стилем.
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
        <div className="w-5 h-5 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 peer-checked:bg-blue-600 peer-checked:border-blue-600 dark:peer-checked:bg-blue-600 dark:peer-checked:border-blue-600 transition-colors flex items-center justify-center shrink-0">
          <svg
            className="w-3.5 h-3.5 text-white opacity-0 peer-checked:opacity-100 transition-opacity"
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
        <span className="text-[14px] font-semibold text-gray-700 dark:text-gray-300 select-none group-hover:text-gray-900 dark:group-hover:text-gray-100 transition-colors">
          Запомнить выбор (Cookies)
        </span>
      </label>
    </div>
  );
}
