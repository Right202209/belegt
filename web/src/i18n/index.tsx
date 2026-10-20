// Spec: 01 §5.4（国际化：de 默认，可切换；数字/日期按界面语言本地化）
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import { de, type MessageKey } from './de';
import { en } from './en';
import { zh } from './zh';
import type { Locale } from '../domain/format';
import type { I18nText } from '../types';

export const LOCALES: Locale[] = ['de', 'en', 'zh'];
export const LOCALE_NAMES: Record<Locale, string> = { de: 'Deutsch', en: 'English', zh: '中文' };
export const DICTIONARIES: Record<Locale, Record<MessageKey, string>> = { de, en, zh };
export const DEFAULT_LOCALE: Locale = 'de';

export type Vars = Record<string, string | number>;

export function translate(locale: Locale, key: MessageKey, vars?: Vars): string {
  const raw = DICTIONARIES[locale][key] ?? DICTIONARIES.de[key] ?? key;
  if (!vars) return raw;
  return raw.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m));
}

/** 取 I18nText 中当前语言，回退 de → en → 任一。 */
export function pickText(t: I18nText | undefined, locale: Locale): string {
  if (!t) return '';
  return t[locale] ?? t.de ?? t.en ?? Object.values(t)[0] ?? '';
}

interface Ctx {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: (key: MessageKey, vars?: Vars) => string;
}

const I18nContext = createContext<Ctx | null>(null);

function initialLocale(): Locale {
  try {
    const saved = localStorage.getItem('belegt.locale');
    if (saved && (LOCALES as string[]).includes(saved)) return saved as Locale;
  } catch {
    /* ignore */
  }
  return DEFAULT_LOCALE;
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(initialLocale);
  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l);
    try {
      localStorage.setItem('belegt.locale', l);
    } catch {
      /* ignore */
    }
    document.documentElement.lang = l === 'zh' ? 'zh-Hans' : l;
  }, []);
  const t = useCallback((key: MessageKey, vars?: Vars) => translate(locale, key, vars), [locale]);
  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): Ctx {
  const c = useContext(I18nContext);
  if (!c) throw new Error('I18nProvider missing');
  return c;
}

export type { MessageKey };
