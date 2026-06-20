export interface SourceBadge {
  bg: string;
  bgDark: string;
  text: string;
  desc: string;
  isNeutral?: boolean;
}

/**
 * Бейджи безопасности для источников OCR.
 * Определяют цвет и описание, открываемое при "отклеивании" стикера.
 */
export function getSourceBadge(id: string): SourceBadge | null {
  if (id === "browser") {
    return {
      bg: "bg-green-500",
      bgDark: "bg-green-600",
      text: "Безопасно.",
      desc: "API относительно безопасны. Локальная обработка. Данные не покидают устройство.",
      isNeutral: false,
    };
  }
  if (id === "auto") {
    return {
      bg: "bg-blue-600",
      bgDark: "bg-blue-600/20",
      text: "Автоматически",
      desc: "Безопасность зависит от выбранного моделью метода (может использовать сеть)\nCloud -> Local -> Browser",
      isNeutral: false,
    };
  }
  if (["local_tess", "local_easy", "gateway"].includes(id)) {
    return {
      bg: "bg-lime-500",
      bgDark: "bg-lime-600",
      text: "Слабо безопасно.",
      desc: "Отправка на сервер предприятия. Безопасность зависит от ваших админов.",
    };
  }
  if (id === "llm") {
    return {
      bg: "bg-red-500",
      bgDark: "bg-red-600",
      text: "Опасность.",
      desc: "Данные отправляются Корпорациям (поставщикам AI) для обработки.",
    };
  }
  return null;
}
