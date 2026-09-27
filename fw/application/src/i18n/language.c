
#include "language.h"

typedef struct {
    const char **strings;
} LanguageData;

const LanguageData const languageData[LANGUAGE_COUNT] = {
    [LANGUAGE_EN_US] = {.strings = lang_en_US},
};

// 当前语言设置 (Current language setting)
Language currentLanguage = LANGUAGE_EN_US;

const char *getLangString(L_StringID stringID) {
    if (stringID >= _L_COUNT) {
        return "@@STR@@";
    }
    if (currentLanguage >= LANGUAGE_COUNT) {
        return lang_en_US[stringID];
    }
    const char *string = languageData[currentLanguage].strings[stringID];
    return string && strlen(string) > 0 ? string : lang_en_US[stringID];
}

void setLanguage(Language lang) { currentLanguage = lang; }

const char *getLangDesc(Language lang) {
    switch (lang) {
        case LANGUAGE_EN_US:
            return "English";
        default:
            return "@@LANG@@";
    }
}

Language getLanguage() { return currentLanguage; }