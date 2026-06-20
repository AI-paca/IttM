interface BugReportLinkProps {
  className?: string;
}

/**
 * "Жук-эпилептик" — SVG-иконка с токсичным черно-синим градиентом,
 * ведущая на issue tracker. Сегмент стилистики AppHeader вынесен отдельно,
 * чтобы не загромождать основной компонент шапки.
 */
export function BugReportLink({ className }: BugReportLinkProps) {
  return (
    <a
      href="https://github.com/AI-paca/IttM/issues"
      target="_blank"
      rel="noreferrer"
      onClick={(e) => e.stopPropagation()}
      title="Сообщить о баге"
      className={`text-[#0a1128] dark:text-[#4facfe] hover:text-blue-500 transition-colors animate-[ping_3s_infinite] sm:animate-none hover:animate-[spin_0.2s_infinite] ${className ?? ""}`}
    >
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width="20"
        height="20"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-label="Сообщить о баге"
      >
        <defs>
          <linearGradient id="toxicBg" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="#0F2027" />
            <stop offset="50%" stopColor="#203A43" />
            <stop offset="100%" stopColor="#2C5364" />
          </linearGradient>
        </defs>
        <path d="m8 2 1.88 1.88" />
        <path d="M14.12 3.88 16 2" />
        <path d="M9 7.13v-1a3.003 3.003 0 1 1 6 0v1" />
        <path
          d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6"
          stroke="url(#toxicBg)"
        />
        <path d="M12 20v-9" />
        <path d="M6.53 9C4.6 8.8 3 7.1 3 5" />
        <path d="M17.47 9c1.93-.2 3.53-1.9 3.53-4" />
        <path d="M8 14H6" />
        <path d="M18 14h-2" />
        <path d="M9 18h-1" />
        <path d="M16 18h-1" />
      </svg>
    </a>
  );
}
