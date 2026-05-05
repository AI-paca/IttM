import React, { useState, useRef, useEffect, DragEvent } from 'react';
import { UploadCloud, RefreshCw, HardDrive, Globe, Search, FileText, X, Copy, Check, Cpu, Moon, Sun, Settings, ChevronDown, Wand2, Cloud } from 'lucide-react';
import { motion, AnimatePresence, useScroll, useMotionValueEvent } from 'motion/react';
import { runBrowserOcrLowMemory } from './lib/browser-ocr';

type AppState = 'upload' | 'configure' | 'loading' | 'reading';
type SourceType = 'auto' | 'gateway' | 'browser' | 'local_tess' | 'local_easy' | 'cdn';

const SOURCES: { id: SourceType; label: string; desc: string; icon: any }[] = [
  { id: 'auto', label: 'Auto (Fallback)', desc: 'Gateway -> Browser fallback', icon: <Wand2 className="w-4 h-4" /> },
  { id: 'gateway', label: 'Gateway API', desc: 'Online / Node+Bun backend', icon: <Cloud className="w-4 h-4" /> },
  { id: 'browser', label: 'Browser Engine', desc: 'WASM On-Device (No backend)', icon: <HardDrive className="w-4 h-4" /> },
  { id: 'local_tess', label: 'Local Tesseract', desc: 'Python API', icon: <Cpu className="w-4 h-4" /> },
  { id: 'local_easy', label: 'Local EasyOCR', desc: 'Python API', icon: <Cpu className="w-4 h-4" /> },
  { id: 'cdn', label: 'CDN Edge', desc: 'Edge processing (WIP)', icon: <Globe className="w-4 h-4" /> },
];

function setCookie(k: string, v: string) {
  document.cookie = `${k}=${v}; path=/; max-age=31536000`;
}
function getCookie(k: string) {
  const match = document.cookie.match(new RegExp('(^| )' + k + '=([^;]+)'));
  return match ? match[2] : null;
}
function delCookie(k: string) {
  document.cookie = `${k}=; path=/; max-age=0`;
}

export default function App() {
  const [appState, setAppState] = useState<AppState>('upload');
  const [file, setFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  
  const [selectedSource, setSelectedSource] = useState<SourceType>('auto');
  
  const [pingUrl, setPingUrl] = useState('');
  const [rememberChoice, setRememberChoice] = useState(false);
  const [showHeader, setShowHeader] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [extractedText, setExtractedText] = useState('');
  const [extractionProgress, setExtractionProgress] = useState('Достаём текст из скриншота...');
  
  const [themeMode, setThemeMode] = useState<'light'|'dark'|'auto'>(() => {
    if (typeof window !== 'undefined') {
       return (localStorage.getItem('theme-mode') as any) || 'auto';
    }
    return 'auto';
  });

  useEffect(() => {
    const savedSource = getCookie('text-extractor-source');
    const savedRemember = getCookie('text-extractor-remember');
    if (savedRemember === 'true') {
      setRememberChoice(true);
      if (savedSource && SOURCES.find(s => s.id === savedSource)) {
        setSelectedSource(savedSource as SourceType);
      }
    }
  }, []);

  useEffect(() => {
    localStorage.setItem('theme-mode', themeMode);
    const applyDark = () => document.documentElement.classList.add('dark');
    const applyLight = () => document.documentElement.classList.remove('dark');
    
    if (themeMode === 'dark') applyDark();
    else if (themeMode === 'light') applyLight();
    else {
      if (window.matchMedia('(prefers-color-scheme: dark)').matches) applyDark();
      else applyLight();
    }
  }, [themeMode]);

  useEffect(() => {
    if (themeMode !== 'auto') return;
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    const handleChange = (e: MediaQueryListEvent) => {
      if (e.matches) document.documentElement.classList.add('dark');
      else document.documentElement.classList.remove('dark');
    };
    mediaQuery.addEventListener('change', handleChange);
    return () => mediaQuery.removeEventListener('change', handleChange);
  }, [themeMode]);

  const handleSourceSelect = (src: SourceType) => {
    setSelectedSource(src);
    if (rememberChoice) {
      setCookie('text-extractor-source', src);
    }
  };

  const handleRememberChange = (checked: boolean) => {
    setRememberChoice(checked);
    if (checked) {
      setCookie('text-extractor-remember', 'true');
      setCookie('text-extractor-source', selectedSource);
    } else {
      delCookie('text-extractor-remember');
      delCookie('text-extractor-source');
    }
  };

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(extractedText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy', err);
    }
  };

  const fileInputRef = useRef<HTMLInputElement>(null);
  const { scrollY } = useScroll();

  useMotionValueEvent(scrollY, "change", () => {
    setShowHeader(true);
  });

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      setFile(e.dataTransfer.files[0]);
      setAppState('configure');
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      setFile(e.target.files[0]);
      setAppState('configure');
    }
  };

  const handleReplaceFile = () => {
    setFile(null);
    setAppState('upload');
  };

  const executeBackendOcr = async (targetFile: File, url: string, activeContent: { current: boolean }) => {
    const formData = new FormData();
    formData.append('file', targetFile);
    if (activeContent.current) setExtractionProgress("Отправка на сервер...");

    const response = await fetch(url, { method: 'POST', body: formData });
    
    if (!response.ok) {
      throw new Error(`Server error: ${response.status}`);
    }
    
    const data = await response.json();
    if (data.error) throw new Error(data.error);

    return data;
  };

  const handleStartExtraction = () => {
    setAppState('loading');
  };

  useEffect(() => {
    let active = { current: true };
    if (appState === 'loading' && file) {
      const runExtract = async () => {
        try {
          let result: any = null;

          if (selectedSource === 'browser') {
            result = await runBrowserOcrLowMemory(file, (text) => {
              if (active.current) setExtractionProgress(text);
            });
          } else if (selectedSource === 'local_tess' || selectedSource === 'local_easy') {
            const engineType = selectedSource === 'local_tess' ? 'tesseract' : 'easyocr';
            let url = new URL('http://127.0.0.1:8000/v1/convert');
            url.searchParams.append('engine_type', engineType);
            result = await executeBackendOcr(file, url.toString(), active);
          } else if (selectedSource === 'gateway' || selectedSource === 'auto') {
            let url = '/api/convert';
            try {
              result = await executeBackendOcr(file, url, active);
            } catch (err: any) {
              if (selectedSource === 'auto') {
                 if (active.current) setExtractionProgress("Gateway недоступен, выполняем локально (WASM)...");
                 result = await runBrowserOcrLowMemory(file, (text) => {
                    if (active.current) setExtractionProgress(text);
                 });
              } else {
                 throw err;
              }
            }
          } else if (selectedSource === 'cdn') {
            throw new Error("CDN strategy is under construction.");
          }

          if (active.current && result) {
            setExtractedText(result.markdown || 'Не удалось распознать текст.');
            setAppState('reading');
          }
        } catch (error: any) {
          if (active.current) {
            alert('Ошибка извлечения: ' + error.message);
            setAppState('configure');
          }
        }
      };
      runExtract();
    }
    return () => {
      active.current = false;
    };
  }, [appState, file, selectedSource]);

  const btnClass = (id: string) => `px-3 py-1.5 text-xs sm:text-sm font-bold rounded-xl transition-all shadow-sm border ${
      selectedSource === id 
        ? 'bg-blue-50 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 border-blue-300 dark:border-blue-700' 
        : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 border-gray-200 dark:border-gray-700 hover:border-blue-300 dark:hover:border-gray-600'
  }`;

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-950 flex flex-col font-sans transition-colors duration-300">
      <AnimatePresence>
        {showHeader && (
          <motion.header
            initial={{ y: 0 }}
            animate={{ y: 0 }}
            exit={{ y: -100 }}
            transition={{ duration: 0.3, ease: "easeInOut" }}
            className={`sticky top-0 z-40 flex justify-center w-full transition-all duration-500 ease-out shadow-sm ${
                appState === 'upload' 
                    ? 'bg-transparent shadow-none dark:bg-transparent' 
                    : 'bg-white/90 dark:bg-gray-900/90 backdrop-blur-xl border-b border-gray-200/80 dark:border-gray-800 shadow-sm'
            }`}
          >
            <div className={`flex items-center justify-between w-full max-w-7xl mx-auto px-4 ${appState === 'upload' ? 'py-4' : 'py-3'}`}>
                {/* Logo & Info */}
                <div className="flex items-center gap-3 flex-shrink-0">
                    <div className="w-9 h-9 bg-blue-600 rounded-xl flex items-center justify-center text-white font-bold shadow-sm">TE</div>
                    <div className="flex flex-col min-w-0 justify-center">
                        <h1 className={`font-bold tracking-tight text-gray-900 dark:text-gray-100 leading-none truncate ${appState === 'upload' ? 'text-xl' : 'text-lg hidden sm:block'}`}>
                            Text Extractor
                        </h1>
                        {appState !== 'upload' && file && (
                            <span className="text-[11px] text-gray-500 dark:text-gray-400 font-semibold truncate max-w-[140px] md:max-w-[200px] mt-0.5 leading-none">
                                {file.name}
                            </span>
                        )}
                    </div>
                </div>

                {/* Source Selection Strip & Config Button */}
                <div className="flex items-center gap-2 ml-auto overflow-hidden">
                   {/* The required strip with Auto first, hiding the rest by breakpoints if needed */}
                   <div className="flex items-center gap-1.5 overflow-x-auto no-scrollbar scroll-smooth">
                      <button onClick={() => handleSourceSelect('auto')} className={btnClass('auto')}>Auto</button>
                      <button onClick={() => handleSourceSelect('browser')} className={`hidden sm:block whitespace-nowrap ${btnClass('browser')}`}>Browser</button>
                      <button onClick={() => handleSourceSelect('local_tess')} className={`hidden md:block whitespace-nowrap ${btnClass('local_tess')}`}>Local Py</button>
                   </div>
                   <button
                      onClick={() => setSidebarOpen(true)}
                      className="p-1 sm:p-1.5 pl-2 sm:pl-3 ml-1 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-200 rounded-xl transition-colors shadow-sm border border-gray-200 dark:border-gray-700/50 shrink-0 flex items-center justify-center gap-2 font-bold text-xs sm:text-sm"
                      title="Выбрать источник"
                   >
                      <div className="hidden sm:block text-blue-600 dark:text-blue-400">
                         {SOURCES.find(s => s.id === selectedSource)?.icon}
                      </div>
                      <span className="truncate max-w-[80px] sm:max-w-[120px]">{SOURCES.find(s => s.id === selectedSource)?.label || 'API'}</span>
                      <ChevronDown className="w-4 h-4 text-gray-500" />
                   </button>
                </div>
            </div>
          </motion.header>
        )}
      </AnimatePresence>

      <main className="flex-1 flex flex-col items-center px-4 py-8 w-full max-w-7xl mx-auto relative z-10">
        <div className="w-full max-w-[800px] flex flex-col gap-6 transition-all duration-500 flex-1">
          
          {appState === 'upload' && (
            <div
              className={`relative flex flex-col items-center justify-center w-full min-h-[300px] md:min-h-[400px] rounded-2xl border-3 border-dashed transition-colors duration-200 cursor-pointer ${
                isDragging ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20' : 'border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 hover:border-blue-400 hover:bg-gray-50 dark:hover:bg-gray-800'
              }`}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
            >
              <input
                type="file"
                ref={fileInputRef}
                className="hidden"
                accept="image/*"
                onChange={handleFileChange}
              />
              <div className="flex flex-col items-center text-center p-6 pointer-events-none">
                <UploadCloud className={`w-16 h-16 mb-4 ${isDragging ? 'text-blue-600' : 'text-gray-400 dark:text-gray-500'}`} />
                <h2 className="text-xl md:text-2xl font-medium text-gray-800 dark:text-gray-100 mb-2">
                  Перетащите скриншот сюда или кликните
                </h2>
                <p className="text-gray-500 dark:text-gray-400 text-sm md:text-base">
                  Поддерживаются изображения PNG, JPG, WEBP
                </p>
              </div>
            </div>
          )}

          {appState === 'configure' && (
            <div className="flex-1 w-full flex flex-col items-center animate-in fade-in duration-500 pt-4 sm:pt-10">
              <div className="absolute top-2 left-2 md:top-8 md:left-0 z-20">
                  <div className="relative group w-[70px] sm:w-[120px] aspect-square bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-2xl shadow-sm flex flex-col items-center justify-center p-2 sm:p-3 transition-all hover:border-blue-300 dark:hover:border-blue-700 mx-auto md:mx-0">
                      <div className="p-1.5 sm:p-3 bg-blue-50 dark:bg-blue-900/30 rounded-xl mb-1 sm:mb-2 flex items-center justify-center">
                          <FileText className="w-5 h-5 sm:w-8 sm:h-8 text-blue-600 dark:text-blue-400" />
                      </div>
                      <span className="text-[7px] sm:text-[10px] font-medium text-gray-600 dark:text-gray-400 truncate w-full text-center px-1" title={file?.name}>
                          {file?.name || 'screenshot.png'}
                      </span>
                      <button
                          onClick={handleReplaceFile}
                          className="absolute -top-2 -right-2 w-6 h-6 sm:w-7 sm:h-7 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-full flex items-center justify-center shadow-md text-gray-400 dark:text-gray-300 hover:text-red-500 dark:hover:text-red-400 transition-all opacity-0 group-hover:opacity-100 scale-90 group-hover:scale-100"
                          title="Заменить файл"
                      >
                          <RefreshCw className="w-3 h-3 sm:w-3.5 sm:h-3.5" />
                      </button>
                      <div className="absolute -bottom-2 px-1.5 py-0.5 bg-green-500 text-white text-[5px] sm:text-[8px] font-bold rounded-full uppercase tracking-wider shadow-sm">
                          Ready
                      </div>
                  </div>
              </div>

              <div className="flex-1" />

              <div className="w-full max-w-[400px] mt-auto flex justify-center z-20 pt-20 pb-8">
                  <button
                      onClick={handleStartExtraction}
                      className="w-full py-4 bg-blue-600 hover:bg-blue-700 text-white text-xl font-bold rounded-2xl shadow-xl shadow-blue-500/20 transition-all active:scale-[0.98] focus:outline-none focus:ring-4 focus:ring-blue-500/50 flex items-center justify-center gap-3 group"
                  >
                      <FileText className="w-6 h-6 outline-none bg-blue-500/50 p-1.5 rounded-lg group-hover:scale-110 transition-transform hidden sm:block backdrop-blur-sm" />
                      Получить текст
                  </button>
              </div>
            </div>
          )}

          {appState === 'loading' && (
            <div className="flex flex-col items-center w-full mt-4 md:mt-8 animate-in fade-in duration-500">
              <div className="flex items-center gap-3 mb-10">
                <div className="w-6 h-6 border-4 border-blue-100 dark:border-blue-900 border-t-blue-600 dark:border-t-blue-400 rounded-full animate-spin"></div>
                <h2 className="text-lg font-semibold text-gray-700 dark:text-gray-200">{extractionProgress}</h2>
              </div>
              
              <div className="w-full max-w-[750px] space-y-6">
                <div className="h-8 bg-gray-200 dark:bg-gray-800 rounded-md w-3/4 animate-pulse"></div>
                <div className="space-y-4">
                  <div className="h-4 bg-gray-200 dark:bg-gray-800 rounded w-full animate-pulse"></div>
                  <div className="h-4 bg-gray-200 dark:bg-gray-800 rounded w-full animate-pulse delay-75"></div>
                  <div className="h-4 bg-gray-200 dark:bg-gray-800 rounded w-11/12 animate-pulse delay-100"></div>
                </div>
              </div>
            </div>
          )}

          {appState === 'reading' && (
            <div className="w-full flex justify-center animate-in fade-in duration-700 pb-20">
              <article className="w-full max-w-[750px] text-gray-900 dark:text-gray-100">
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-8 sm:mb-10 px-4 md:px-0">
                  <h1 className="text-2xl sm:text-3xl font-bold leading-tight tracking-tight text-gray-950 dark:text-gray-50">
                    Извлеченный текст
                  </h1>
                  <button 
                    onClick={handleCopy}
                    className={`hidden sm:flex flex-shrink-0 items-center justify-center gap-2 px-4 py-2.5 rounded-xl font-bold text-sm transition-all active:scale-95 border ${
                      copied 
                        ? 'bg-green-50 dark:bg-green-900/30 border-green-200 dark:border-green-800 text-green-700 dark:text-green-400 shadow-sm' 
                        : 'bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-750 text-gray-700 dark:text-gray-300 border-gray-200 dark:border-gray-700 shadow-sm'
                    }`}
                  >
                    {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4 text-gray-400 dark:text-gray-500" />}
                    {copied ? 'Скопировано!' : 'Копировать всё'}
                  </button>
                </div>
                
                <div className="text-[17px] sm:text-[18px] leading-[1.7] sm:leading-[1.8] space-y-[24px] sm:space-y-[28px] text-gray-800 dark:text-gray-300 selection:bg-blue-100 dark:selection:bg-blue-900 pb-32 md:pb-12 px-4 md:px-0 whitespace-pre-wrap">
                  {extractedText}
                </div>
                
                <div className="fixed bottom-0 left-0 right-0 p-4 pb-6 bg-gradient-to-t from-white dark:from-gray-950 via-white dark:via-gray-950 to-white/0 dark:to-transparent md:static md:bg-none md:p-0 md:mt-16 md:flex md:flex-row items-center md:border-t md:border-gray-100 dark:md:border-gray-800 md:pt-8 z-30">
                   <div className="flex gap-3 max-w-[750px] mx-auto w-full px-0 md:px-0">
                      <button
                        onClick={() => {
                            setAppState('upload');
                            setFile(null);
                        }}
                        className="w-14 md:w-auto flex-shrink-0 md:flex-none py-4 md:py-3 px-0 md:px-6 text-center text-gray-700 dark:text-gray-300 font-bold bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-xl hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors shadow-sm text-[15px] flex items-center justify-center gap-2 active:scale-95"
                      >
                        <RefreshCw className="w-5 h-5 md:w-4 md:h-4 text-gray-600 dark:text-gray-400" />
                        <span className="hidden md:inline">Новый скриншот</span>
                      </button>
                      <button
                        onClick={handleCopy}
                        className={`flex-1 md:hidden py-4 px-2 whitespace-nowrap text-center font-bold rounded-xl transition-all shadow-lg text-[15px] flex items-center justify-center gap-2 active:scale-95 ${
                          copied 
                            ? 'bg-green-500 text-white shadow-green-500/20' 
                            : 'bg-blue-600 hover:bg-blue-700 text-white shadow-blue-500/30'
                        }`}
                      >
                        {copied ? <Check className="w-5 h-5 flex-shrink-0" /> : <Copy className="w-5 h-5 flex-shrink-0" />}
                        {copied ? 'Скопировано' : 'Копировать'}
                      </button>
                   </div>
                </div>
              </article>
            </div>
          )}

        </div>
      </main>

      {/* Sidebar Overlay */}
      <AnimatePresence>
        {sidebarOpen && (
            <>
              <motion.div 
                initial={{ opacity: 0 }} 
                animate={{ opacity: 1 }} 
                exit={{ opacity: 0 }}
                className="fixed inset-0 bg-gray-900/40 z-[90] backdrop-blur-sm"
                onClick={() => setSidebarOpen(false)}
              />
              <motion.div
                initial={{ x: '100%' }} 
                animate={{ x: 0 }} 
                exit={{ x: '100%' }}
                transition={{ type: 'spring', damping: 25, stiffness: 200 }}
                className="fixed top-0 right-0 bottom-0 w-[85%] max-w-[340px] bg-white dark:bg-gray-900 shadow-2xl z-[100] border-l border-gray-200 dark:border-gray-800 flex flex-col font-sans"
              >
                <div className="flex items-center justify-between p-4 sm:p-5 border-b border-gray-100 dark:border-gray-800 bg-gray-50/50 dark:bg-gray-900 shrink-0">
                    <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                      <Settings className="w-5 h-5 text-gray-500 dark:text-gray-400" />
                      Настройки
                    </h2>
                    <button onClick={() => setSidebarOpen(false)} className="p-1.5 text-gray-500 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-800 hover:text-gray-900 dark:hover:text-gray-100 rounded-lg transition-colors">
                      <X className="w-5 h-5"/>
                    </button>
                </div>
                
                <div className="p-4 sm:p-5 flex flex-col gap-6 overflow-y-auto">
                    {/* Source Selection List */}
                    <div className="flex flex-col gap-4">
                        
                        {/* Segment 1: Non-API */}
                        <div className="flex flex-col gap-2">
                            <h3 className="text-[10px] font-bold text-gray-400 uppercase tracking-widest pl-1">Local & Browser</h3>
                           {SOURCES.filter(s => ['auto', 'browser', 'local_tess', 'local_easy'].includes(s.id)).map(src => (
                               <button
                                   key={src.id}
                                   onClick={() => handleSourceSelect(src.id as SourceType)}
                                   className={`w-full flex items-center justify-between px-3 py-3 text-left transition-colors rounded-xl border ${
                                       selectedSource === src.id 
                                           ? 'bg-blue-50/50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800' 
                                           : 'bg-white dark:bg-gray-800 border-gray-100 dark:border-gray-700 hover:border-blue-300 dark:hover:border-gray-600'
                                   }`}
                               >
                                   <div className="flex items-center gap-3">
                                       <div className={selectedSource === src.id ? "text-blue-600 dark:text-blue-400" : "text-gray-400 dark:text-gray-500"}>
                                           {src.icon}
                                       </div>
                                       <div className="flex flex-col">
                                           <span className={`text-[13px] font-semibold ${selectedSource === src.id ? "text-blue-700 dark:text-blue-300" : "text-gray-700 dark:text-gray-200"}`}>
                                               {src.label}
                                           </span>
                                           <span className="text-[11px] text-gray-500 dark:text-gray-400 leading-tight mt-0.5">{src.desc}</span>
                                       </div>
                                   </div>
                                   {selectedSource === src.id && <Check className="w-4 h-4 text-blue-600 dark:text-blue-400 shrink-0" />}
                               </button>
                           ))}
                        </div>

                        {/* Segment 2: API */}
                        <div className="flex flex-col gap-2 mt-2">
                           <h3 className="text-[10px] font-bold text-gray-400 uppercase tracking-widest pl-1">API & Cloud</h3>
                           {SOURCES.filter(s => ['gateway', 'cdn'].includes(s.id)).map(src => (
                               <button
                                   key={src.id}
                                   onClick={() => handleSourceSelect(src.id as SourceType)}
                                   className={`w-full flex items-center justify-between px-3 py-3 text-left transition-colors rounded-xl border ${
                                       selectedSource === src.id 
                                           ? 'bg-blue-50/50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800' 
                                           : 'bg-white dark:bg-gray-800 border-gray-100 dark:border-gray-700 hover:border-blue-300 dark:hover:border-gray-600'
                                   }`}
                               >
                                   <div className="flex items-center gap-3">
                                       <div className={selectedSource === src.id ? "text-blue-600 dark:text-blue-400" : "text-gray-400 dark:text-gray-500"}>
                                           {src.icon}
                                       </div>
                                       <div className="flex flex-col">
                                           <span className={`text-[13px] font-semibold ${selectedSource === src.id ? "text-blue-700 dark:text-blue-300" : "text-gray-700 dark:text-gray-200"}`}>
                                               {src.label}
                                           </span>
                                           <span className="text-[11px] text-gray-500 dark:text-gray-400 leading-tight mt-0.5">{src.desc}</span>
                                       </div>
                                   </div>
                                   {selectedSource === src.id && <Check className="w-4 h-4 text-blue-600 dark:text-blue-400 shrink-0" />}
                               </button>
                           ))}
                        </div>
                    </div>

                    <div className="h-px bg-gray-100 dark:bg-gray-800" />

                    {/* Remember Choice */}
                    <div className="flex flex-col gap-3">
                        <label className="relative flex items-center gap-3 cursor-pointer group p-3 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 hover:border-blue-300 dark:hover:border-gray-600 transition-colors">
                            <input
                                type="checkbox"
                                className="peer sr-only"
                                checked={rememberChoice}
                                onChange={(e) => handleRememberChange(e.target.checked)}
                            />
                            <div className="w-5 h-5 border-2 border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 peer-checked:bg-blue-600 peer-checked:border-blue-600 dark:peer-checked:bg-blue-600 dark:peer-checked:border-blue-600 transition-colors flex items-center justify-center shrink-0">
                                <svg className="w-3.5 h-3.5 text-white opacity-0 peer-checked:opacity-100 transition-opacity" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                                </svg>
                            </div>
                            <div className="flex flex-col">
                              <span className="text-[13px] font-semibold text-gray-700 dark:text-gray-300 select-none group-hover:text-gray-900 dark:group-hover:text-gray-100 transition-colors">
                                  Запомнить выбор
                              </span>
                              <span className="text-[11px] text-gray-500 dark:text-gray-400 leading-tight mt-0.5">
                                  Сохранить выбранный источник обработки между сессиями (Cookies)
                              </span>
                            </div>
                        </label>
                    </div>

                    {/* API Settings */}
                    <div className="flex flex-col gap-3">
                        <h3 className="text-[10px] font-bold text-gray-400 uppercase tracking-widest pl-1 mt-2">Дополнительно</h3>
                        <div className="flex flex-col">
                            <label className="text-[11px] font-semibold text-gray-600 dark:text-gray-400 mb-1 pl-1">Пользовательский API Gateway</label>
                            <input
                                type="url"
                                placeholder="https://my-gateway.com/convert"
                                value={pingUrl}
                                onChange={(e) => setPingUrl(e.target.value)}
                                className="flex-1 w-full p-2.5 border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 rounded-xl text-sm focus:ring-2 focus:ring-blue-500 outline-none text-gray-800 dark:text-gray-200 transition-all font-mono shadow-sm"
                            />
                        </div>
                    </div>

                    <div className="flex-1" />

                    <div className="h-px bg-gray-100 dark:bg-gray-800 mt-4" />

                    {/* Theme Segmented Control in bottom */}
                    <div className="flex flex-col gap-3 pb-4">
                        <h3 className="text-[10px] font-bold text-gray-400 uppercase tracking-widest pl-1">Тема оформления</h3>
                        <div className="flex bg-gray-100 dark:bg-gray-800 p-1 rounded-xl w-full">
                            <button 
                               onClick={() => setThemeMode('auto')} 
                               className={`flex-1 py-2 text-xs font-bold rounded-lg transition-all ${themeMode === 'auto' ? 'bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white' : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}`}
                            >Default</button>
                            <button 
                               onClick={() => setThemeMode('light')} 
                               className={`flex-1 py-2 text-xs font-bold rounded-lg transition-all ${themeMode === 'light' ? 'bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white' : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}`}
                            >Light</button>
                            <button 
                               onClick={() => setThemeMode('dark')} 
                               className={`flex-1 py-2 text-xs font-bold rounded-lg transition-all ${themeMode === 'dark' ? 'bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white' : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}`}
                            >Dark</button>
                        </div>
                    </div>
                </div>
              </motion.div>
            </>
        )}
      </AnimatePresence>
    </div>
  );
}
