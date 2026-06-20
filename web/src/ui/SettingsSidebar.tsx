import { useRef, useState } from "react";
import { X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import type { Variants } from "motion/react";
import type { EngineControls } from "./layout/engine-controls.types";
import { SOURCES } from "./sources";
import { GatewayPanel } from "./settings/GatewayPanel";
import { LlmPanel } from "./settings/LlmPanel";
import { OverscrollReveal } from "./settings/OverscrollReveal";
import { RememberToggle } from "./settings/RememberToggle";
import { SourceCard } from "./settings/SourceCard";
import { ThemeToggle } from "./settings/ThemeToggle";
import { useIsMobile } from "./settings/useIsMobile";
import { useOverscrollReveal } from "./settings/useOverscrollReveal";

interface SettingsSidebarProps {
  controls: EngineControls;
  isOpen: boolean;
  onClose: () => void;
}

const LOCAL_SOURCE_IDS = ["auto", "browser", "local_tess", "local_easy"];
const CLOUD_SOURCE_IDS = ["gateway", "llm"];

/**
 * Сайдбар настроек: выбор источника OCR, конфигурация Gateway/LLM,
 * переключатель темы и опция запоминания выбора.
 *
 * Раньше был "божественным" компонентом (~615 строк) — теперь разбит на
 * одноотвественные модули в `./settings/`. Здесь только оркестрация:
 * мобильный bottom-sheet, overscroll reveal и композиция секций.
 *
 * Рефакторинг: хардкодные gray-* классы заменены на семантические токены
 * (bg-surface, bg-overlay, text-faint, bg-strong, border-default).
 */
export function SettingsSidebar({
  controls,
  isOpen,
  onClose,
}: SettingsSidebarProps) {
  const {
    easyOcrInstallMessage,
    easyOcrInstallProgress,
    easyOcrInstalling,
    pingUrl,
    rememberChoice,
    selectedSource,
    themeMode,
    onInstallEasyOcr,
    onRememberChange,
    onSourceSelect,
    setPingUrl,
    setThemeMode,
  } = controls;

  const isMobile = useIsMobile();
  const [peeledId, setPeeledId] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const { overscroll, isRevealed, touchActive, handlers, close } =
    useOverscrollReveal(scrollRef);

  const togglePeel = (id: string) => setPeeledId(id || null);

  const renderSource = (id: string) => {
    const src = SOURCES.find((s) => s.id === id);
    if (!src) return null;
    return (
      <SourceCard
        key={src.id}
        src={src}
        isMobile={isMobile}
        isActive={selectedSource === src.id}
        isPeeled={peeledId === src.id}
        easyOcrInstalling={easyOcrInstalling}
        easyOcrInstallMessage={easyOcrInstallMessage}
        easyOcrInstallProgress={easyOcrInstallProgress}
        onSelect={onSourceSelect}
        onInstallEasyOcr={onInstallEasyOcr}
        onTogglePeel={togglePeel}
      />
    );
  };

  const sidebarVariants: Variants = {
    initial: {
      y: isMobile ? "100%" : 0,
      x: isMobile ? 0 : "100%",
    },
    animate: {
      y: 0,
      x: 0,
      transition: { type: "spring", damping: 28, stiffness: 220 },
    },
    exit: {
      y: isMobile ? "100%" : 0,
      x: isMobile ? 0 : "100%",
      transition: {
        type: "tween",
        duration: 0.24,
        ease: [0.32, 0, 0.67, 0],
      },
    },
  };

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.24, ease: "easeInOut" }}
            className="fixed inset-0 bg-overlay z-[90] backdrop-blur-sm"
            onClick={onClose}
          />
          <motion.div
            variants={sidebarVariants}
            initial="initial"
            animate="animate"
            exit="exit"
            className={`fixed ${
              isMobile
                ? "bottom-0 left-0 right-0 h-auto max-h-[85vh] rounded-t-3xl border-t"
                : "top-0 right-0 bottom-0 w-[90%] max-w-[360px] border-l"
            } bg-surface shadow-2xl z-[100] border-default flex flex-col font-sans`}
          >
            {isMobile && (
              <div
                className="w-full flex justify-center pt-3 pb-1"
                onClick={onClose}
              >
                <div className="w-12 h-1.5 bg-strong rounded-full" />
              </div>
            )}
            <button
              onClick={onClose}
              className="absolute top-4 right-4 p-1.5 text-faint hover:text-primary bg-elevated hover:bg-inset rounded-full transition-colors z-[110]"
            >
              <X className="w-6 h-6" />
            </button>

            <div
              ref={scrollRef}
              className="flex-1 overflow-y-auto no-scrollbar relative z-10 bg-surface"
              onScroll={handlers.onScroll}
              onWheel={handlers.onWheel}
              onTouchStart={handlers.onTouchStart}
              onTouchMove={handlers.onTouchMove}
              onTouchEnd={handlers.onTouchEnd}
            >
              <div
                className="flex flex-col min-h-full"
                style={{
                  transform: `translateY(-${overscroll}px)`,
                  transition: touchActive
                    ? "none"
                    : "transform 0.3s cubic-bezier(0.25, 1, 0.5, 1)",
                }}
              >
                <div className="p-4 sm:p-6 pt-14 flex flex-col gap-4 sm:gap-6 pb-6 flex-1">
                  {/* Верхняя секция: источники + конфигурация. flex-1 растягивает,
                      чтобы нижние контролы (тема/запоминание) прижимались к низу
                      сайдбара, когда контента мало. */}
                  <div className="flex flex-col gap-4 flex-1">
                    <section className="flex flex-col gap-2">
                      <h3 className="text-[10px] font-bold text-faint uppercase tracking-widest pl-1">
                        Local & Browser
                      </h3>
                      {LOCAL_SOURCE_IDS.map(renderSource)}
                    </section>

                    <section className="flex flex-col gap-2 mt-2">
                      <h3 className="text-[10px] font-bold text-faint uppercase tracking-widest pl-1">
                        API & Cloud
                      </h3>
                      {CLOUD_SOURCE_IDS.map(renderSource)}

                      {selectedSource === "gateway" && (
                        <GatewayPanel
                          pingUrl={pingUrl}
                          setPingUrl={setPingUrl}
                        />
                      )}

                      {selectedSource === "llm" && (
                        <LlmPanel controls={controls} />
                      )}
                    </section>
                  </div>

                  <div className="divider" />

                  {/* Нижняя секция: прижата к низу сайдбара */}
                  <div className="flex flex-col gap-3 shrink-0">
                    <RememberToggle
                      checked={rememberChoice}
                      onChange={onRememberChange}
                    />
                    <ThemeToggle
                      themeMode={themeMode}
                      onChange={setThemeMode}
                    />
                  </div>
                </div>

                <OverscrollReveal
                  isRevealed={isRevealed}
                  overscroll={overscroll}
                  touchActive={touchActive}
                  onClose={close}
                />
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}