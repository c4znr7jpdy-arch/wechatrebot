"""
天气 SVG 图标 — 内联 SVG 模板，带渐变和阴影，视觉效果接近 3D
"""


def svg_sun(size: int = 64) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">
  <defs>
    <radialGradient id="sg" cx="45%" cy="40%"><stop offset="0%" stop-color="#FFE066"/><stop offset="100%" stop-color="#FFB800"/></radialGradient>
    <filter id="ss"><feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#FF9500" flood-opacity="0.35"/></filter>
  </defs>
  <g filter="url(#ss)">
    <circle cx="50" cy="50" r="20" fill="url(#sg)"/>
    <g stroke="#FFB800" stroke-width="3" stroke-linecap="round" opacity="0.85">
      <line x1="50" y1="10" x2="50" y2="20"/><line x1="50" y1="80" x2="50" y2="90"/>
      <line x1="10" y1="50" x2="20" y2="50"/><line x1="80" y1="50" x2="90" y2="50"/>
      <line x1="21.7" y1="21.7" x2="28.8" y2="28.8"/><line x1="71.2" y1="71.2" x2="78.3" y2="78.3"/>
      <line x1="78.3" y1="21.7" x2="71.2" y2="28.8"/><line x1="28.8" y1="71.2" x2="21.7" y2="78.3"/>
    </g>
  </g>
</svg>'''


def svg_cloud(size: int = 64, alpha: float = 1.0) -> str:
    op = f' opacity="{alpha}"' if alpha < 1 else ""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">
  <defs>
    <linearGradient id="cg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#fff" stop-opacity="0.95"/>
      <stop offset="100%" stop-color="#e8ecf0" stop-opacity="0.85"/>
    </linearGradient>
    <filter id="cs"><feDropShadow dx="0" dy="3" stdDeviation="4" flood-color="#6b7b8d" flood-opacity="0.25"/></filter>
  </defs>
  <g filter="url(#cs)"{op}>
    <ellipse cx="50" cy="58" rx="34" ry="18" fill="url(#cg)"/>
    <circle cx="32" cy="42" r="18" fill="url(#cg)"/>
    <circle cx="58" cy="38" r="14" fill="url(#cg)"/>
    <circle cx="45" cy="34" r="11" fill="url(#cg)"/>
  </g>
</svg>'''


def svg_partly_cloudy(size: int = 64) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">
  <defs>
    <radialGradient id="psg" cx="45%" cy="40%"><stop offset="0%" stop-color="#FFE066"/><stop offset="100%" stop-color="#FFB800"/></radialGradient>
    <linearGradient id="pcg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#fff" stop-opacity="0.95"/>
      <stop offset="100%" stop-color="#e8ecf0" stop-opacity="0.85"/>
    </linearGradient>
    <filter id="pcs"><feDropShadow dx="0" dy="3" stdDeviation="4" flood-color="#6b7b8d" flood-opacity="0.25"/></filter>
  </defs>
  <g>
    <!-- 太阳：左上角，更大更明显 -->
    <circle cx="30" cy="28" r="18" fill="url(#psg)"/>
    <g stroke="#FFB800" stroke-width="3" stroke-linecap="round" opacity="0.8">
      <line x1="30" y1="2" x2="30" y2="10"/>
      <line x1="30" y1="46" x2="30" y2="54"/>
      <line x1="4" y1="28" x2="12" y2="28"/>
      <line x1="48" y1="28" x2="56" y2="28"/>
      <line x1="11.6" y1="9.6" x2="17.3" y2="15.3"/>
      <line x1="42.7" y1="9.6" x2="37" y2="15.3"/>
      <line x1="42.7" y1="46.4" x2="37" y2="40.7"/>
      <line x1="11.6" y1="46.4" x2="17.3" y2="40.7"/>
    </g>
    <!-- 云：右下角，较小 -->
    <g filter="url(#pcs)">
      <ellipse cx="60" cy="65" rx="28" ry="14" fill="url(#pcg)"/>
      <circle cx="44" cy="52" r="14" fill="url(#pcg)"/>
      <circle cx="64" cy="48" r="11" fill="url(#pcg)"/>
      <circle cx="54" cy="44" r="9" fill="url(#pcg)"/>
    </g>
  </g>
</svg>'''


def svg_overcast(size: int = 64) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">
  <defs>
    <linearGradient id="oc1" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#c8cdd3" stop-opacity="0.7"/>
      <stop offset="100%" stop-color="#b0b8c2" stop-opacity="0.6"/>
    </linearGradient>
    <linearGradient id="oc2" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#fff" stop-opacity="0.9"/>
      <stop offset="100%" stop-color="#e0e4ea" stop-opacity="0.8"/>
    </linearGradient>
    <filter id="ocs"><feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#6b7b8d" flood-opacity="0.2"/></filter>
  </defs>
  <g filter="url(#ocs)">
    <ellipse cx="42" cy="48" rx="28" ry="14" fill="url(#oc1)"/>
    <circle cx="28" cy="36" r="14" fill="url(#oc1)"/>
    <circle cx="50" cy="33" r="11" fill="url(#oc1)"/>
    <ellipse cx="55" cy="62" rx="32" ry="16" fill="url(#oc2)"/>
    <circle cx="38" cy="48" r="16" fill="url(#oc2)"/>
    <circle cx="62" cy="44" r="13" fill="url(#oc2)"/>
    <circle cx="50" cy="40" r="10" fill="url(#oc2)"/>
  </g>
</svg>'''


def svg_rain(size: int = 64) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">
  <defs>
    <linearGradient id="rcg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#d0d6de"/><stop offset="100%" stop-color="#b8c0cc"/>
    </linearGradient>
    <linearGradient id="rdg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#7eb8f0"/><stop offset="100%" stop-color="#5a9ad8"/>
    </linearGradient>
    <filter id="rs"><feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#4a7ab5" flood-opacity="0.3"/></filter>
  </defs>
  <g>
    <g filter="url(#rs)">
      <ellipse cx="50" cy="40" rx="30" ry="16" fill="url(#rcg)"/>
      <circle cx="34" cy="28" r="15" fill="url(#rcg)"/>
      <circle cx="58" cy="25" r="12" fill="url(#rcg)"/>
      <circle cx="46" cy="22" r="10" fill="url(#rcg)"/>
    </g>
    <g fill="url(#rdg)" opacity="0.85">
      <ellipse cx="32" cy="64" rx="3" ry="5" transform="rotate(-15 32 64)"/>
      <ellipse cx="46" cy="70" rx="3" ry="5" transform="rotate(-15 46 70)"/>
      <ellipse cx="60" cy="62" rx="3" ry="5" transform="rotate(-15 60 62)"/>
      <ellipse cx="38" cy="78" rx="2.5" ry="4" transform="rotate(-15 38 78)"/>
      <ellipse cx="54" cy="80" rx="2.5" ry="4" transform="rotate(-15 54 80)"/>
    </g>
  </g>
</svg>'''


def svg_light_rain(size: int = 64) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">
  <defs>
    <linearGradient id="lrcg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#d0d6de"/><stop offset="100%" stop-color="#b8c0cc"/>
    </linearGradient>
    <linearGradient id="lrdg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#8ec4f5"/><stop offset="100%" stop-color="#6aadda"/>
    </linearGradient>
    <filter id="lrs"><feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#4a7ab5" flood-opacity="0.25"/></filter>
  </defs>
  <g>
    <g filter="url(#lrs)">
      <ellipse cx="50" cy="40" rx="30" ry="16" fill="url(#lrcg)"/>
      <circle cx="34" cy="28" r="15" fill="url(#lrcg)"/>
      <circle cx="58" cy="25" r="12" fill="url(#lrcg)"/>
      <circle cx="46" cy="22" r="10" fill="url(#lrcg)"/>
    </g>
    <g fill="url(#lrdg)" opacity="0.7">
      <ellipse cx="38" cy="66" rx="2.5" ry="4.5" transform="rotate(-15 38 66)"/>
      <ellipse cx="56" cy="72" rx="2.5" ry="4.5" transform="rotate(-15 56 72)"/>
    </g>
  </g>
</svg>'''


def svg_shower(size: int = 64) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">
  <defs>
    <radialGradient id="shsg" cx="45%" cy="40%"><stop offset="0%" stop-color="#FFE066"/><stop offset="100%" stop-color="#FFB800"/></radialGradient>
    <linearGradient id="shcg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#d0d6de"/><stop offset="100%" stop-color="#b8c0cc"/>
    </linearGradient>
    <linearGradient id="shdg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#7eb8f0"/><stop offset="100%" stop-color="#5a9ad8"/>
    </linearGradient>
  </defs>
  <g>
    <circle cx="68" cy="24" r="12" fill="url(#shsg)" opacity="0.85"/>
    <g stroke="#FFB800" stroke-width="2" stroke-linecap="round" opacity="0.6">
      <line x1="68" y1="6" x2="68" y2="11"/><line x1="82" y1="10" x2="79" y2="14"/>
      <line x1="86" y1="24" x2="81" y2="24"/>
    </g>
    <g>
      <ellipse cx="46" cy="44" rx="30" ry="16" fill="url(#shcg)"/>
      <circle cx="30" cy="32" r="15" fill="url(#shcg)"/>
      <circle cx="54" cy="28" r="12" fill="url(#shcg)"/>
      <circle cx="42" cy="26" r="10" fill="url(#shcg)"/>
    </g>
    <g fill="url(#shdg)" opacity="0.8">
      <ellipse cx="34" cy="66" rx="3" ry="5" transform="rotate(-15 34 66)"/>
      <ellipse cx="52" cy="70" rx="3" ry="5" transform="rotate(-15 52 70)"/>
    </g>
  </g>
</svg>'''


def svg_snow(size: int = 64) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">
  <defs>
    <linearGradient id="scg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#d8dce4"/><stop offset="100%" stop-color="#c0c8d4"/>
    </linearGradient>
    <filter id="sss"><feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#8899aa" flood-opacity="0.2"/></filter>
  </defs>
  <g>
    <g filter="url(#sss)">
      <ellipse cx="50" cy="38" rx="30" ry="16" fill="url(#scg)"/>
      <circle cx="34" cy="26" r="15" fill="url(#scg)"/>
      <circle cx="58" cy="23" r="12" fill="url(#scg)"/>
      <circle cx="46" cy="20" r="10" fill="url(#scg)"/>
    </g>
    <g fill="#e8f0ff" opacity="0.9">
      <circle cx="34" cy="64" r="4"/><circle cx="50" cy="72" r="3.5"/>
      <circle cx="64" cy="62" r="4"/><circle cx="42" cy="80" r="3"/>
      <circle cx="58" cy="82" r="3"/>
    </g>
    <g stroke="#d0e0f5" stroke-width="1.5" stroke-linecap="round" opacity="0.7">
      <line x1="34" y1="60" x2="34" y2="68"/><line x1="30" y1="64" x2="38" y2="64"/>
      <line x1="50" y1="68" x2="50" y2="76"/><line x1="46" y1="72" x2="54" y2="72"/>
      <line x1="64" y1="58" x2="64" y2="66"/><line x1="60" y1="62" x2="68" y2="62"/>
    </g>
  </g>
</svg>'''


def svg_thunder(size: int = 64) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">
  <defs>
    <linearGradient id="tcg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#8090a8"/><stop offset="100%" stop-color="#607088"/>
    </linearGradient>
    <linearGradient id="tbg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#FFE066"/><stop offset="100%" stop-color="#FF9500"/>
    </linearGradient>
    <filter id="ts"><feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#FF6600" flood-opacity="0.3"/></filter>
  </defs>
  <g>
    <g>
      <ellipse cx="50" cy="36" rx="30" ry="16" fill="url(#tcg)"/>
      <circle cx="34" cy="24" r="15" fill="url(#tcg)"/>
      <circle cx="58" cy="21" r="12" fill="url(#tcg)"/>
      <circle cx="46" cy="18" r="10" fill="url(#tcg)"/>
    </g>
    <g filter="url(#ts)">
      <polygon points="52,48 40,68 48,68 44,88 62,62 52,62 58,48" fill="url(#tbg)"/>
    </g>
  </g>
</svg>'''


def svg_fog(size: int = 64) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">
  <defs>
    <filter id="fs"><feGaussianBlur stdDeviation="1.5"/></filter>
  </defs>
  <g filter="url(#fs)" opacity="0.7">
    <rect x="20" y="30" width="60" height="6" rx="3" fill="#d0d8e0"/>
    <rect x="14" y="44" width="72" height="6" rx="3" fill="#c0c8d2"/>
    <rect x="24" y="58" width="52" height="6" rx="3" fill="#d0d8e0"/>
    <rect x="18" y="72" width="64" height="6" rx="3" fill="#c0c8d2"/>
  </g>
</svg>'''


# ── 工具函数 ────────────────────────────────────────
def get_svg_icon(weather_code: str, size: int = 64) -> str:
    code = weather_code.lower() if weather_code else ""
    if "lei" in code:
        return svg_thunder(size)
    elif "zhenyu" in code:
        return svg_shower(size)
    elif "xiaoyu" in code:
        return svg_light_rain(size)
    elif "duoyun" in code:
        return svg_partly_cloudy(size)
    elif "yu" in code:
        return svg_rain(size)
    elif "xue" in code:
        return svg_snow(size)
    elif "wu" in code or "mai" in code:
        return svg_fog(size)
    elif "qing" in code:
        return svg_sun(size)
    elif "yin" in code:
        return svg_overcast(size)
    else:
        return svg_cloud(size)
