#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef LANGUAGE_H
#define LANGUAGE_H

#include "string_id.h"

#define _T(x) getLangString(_L_##x)


typedef enum {
    // other languages are removed from this build to save flash
    LANGUAGE_EN_US,
    LANGUAGE_COUNT
} Language;

extern const char* lang_en_US[_L_COUNT];

// 获取字符串的函数 (Get language string function)
const char* getLangString(L_StringID stringID);
void setLanguage(Language lang);
Language getLanguage();
const char* getLangDesc(Language lang);


#endif // LANGUAGE_H
