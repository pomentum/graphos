import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import en from "./en.json";

/**
 * The UI's text. Every label, message and hint lives in `en.json`, keyed by
 * where it is used, layer first:
 *
 *   common.*                    words shared by many screens (Save, Cancel, …)
 *   shell.*                     the frame: toolbar, sidebar, sign-in, account
 *   views.<view>.<area>.<name>  one screen, e.g. views.explore.actions.back
 *   components.<name>.*         a reusable component, e.g. components.inspector.hide
 *   notices.*                   short confirmations and toasts
 *   errors.*                    what went wrong, in the user's words
 *
 * Components read text with `useTranslation()`; code outside React (models,
 * query hooks) imports `t` from here. Counts use i18next plurals (`_one` /
 * `_other`) and values are interpolated as `{{name}}`. Keys are typed from
 * `en.json`, so a misspelt key does not compile.
 */
void i18n.use(initReactI18next).init({
  resources: { en: { translation: en } },
  lng: "en",
  fallbackLng: "en",
  interpolation: { escapeValue: false }, // React escapes already
  returnNull: false,
  showSupportNotice: false, // no promotional console line in the app or the tests
});

export const t = i18n.t.bind(i18n);
export default i18n;
