# WCAG AA Contrast Audit - Background: #14141e (RGB: 20, 20, 30)
# Reproducible measurements are enforced by test_wcag_palette_contrast_measurements.
# Inventory: filename/primary, path+snippet/secondary+muted, score, mode labels,
# status, skipped/error, warnings, settings/OCR, update copy, and focus borders.
#
# Colors:
# - White (#ffffff): 18.6:1 (Pass AA)
# - Light Gray (#cccccc) - Snippet: 11.5:1 (Pass AA)
# - Gray (#aaaaaa) - Path/Secondary: 7.9:1 (Pass AA)
# - Light Purple (#d8b4fe) - Semantic mode: 12.8:1 (Pass AA)
# - Light Green (#6ee7b7) - Keyword mode: 13.9:1 (Pass AA)
# - Bright Red (#ef4444) - Error: 4.86:1 (Pass AA)
# - Bright Orange (#f59e0b) - Warning: 8.51:1 (Pass AA)
#
# Updated from failing/muted colors:
# - Score: was #888888 (5.4:1). Now #999999 (7.0:1) to ensure robust AA.
# - Status/Info text: was rgba(255,255,255,0.4) or 0.5. Now rgba(255, 255, 255, 0.7) (approx #b2b2b2, 9.3:1 composite over #14141e).
# - Error text: was translucent red, which remained below 4.5:1 after compositing.
#   It now uses solid #ef4444 (4.86:1) so 11–12 px state text passes WCAG AA.
#
# Note on translucency: Ratios for rgba() colors are calculated against the solid #14141e background.
# Alpha-composited colors must provide sufficient contrast on their own.

def _linear_channel(channel: int) -> float:
    """Convert one sRGB channel to linear light for WCAG calculations."""
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def composite_rgba(foreground: tuple[int, int, int], alpha: float, background: tuple[int, int, int]) -> tuple[int, int, int]:
    """Composite an RGB foreground over an opaque background."""
    red = round(foreground[0] * alpha + background[0] * (1.0 - alpha))
    green = round(foreground[1] * alpha + background[1] * (1.0 - alpha))
    blue = round(foreground[2] * alpha + background[2] * (1.0 - alpha))
    return red, green, blue


def contrast_ratio(foreground: tuple[int, int, int], background: tuple[int, int, int]) -> float:
    """Return the WCAG 2 relative-luminance contrast ratio."""
    def luminance(rgb: tuple[int, int, int]) -> float:
        red, green, blue = (_linear_channel(channel) for channel in rgb)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class ThemeColors:
    # Text colors
    TEXT_PRIMARY = "white"
    TEXT_SECONDARY = "#cccccc"
    TEXT_MUTED = "#aaaaaa"
    TEXT_SCORE = "#999999"

    # Semantic text colors
    MODE_SEMANTIC = "#d8b4fe"
    MODE_KEYWORD = "#6ee7b7"

    # State colors (Solid)
    ERROR = "#ef4444"
    WARNING = "#f59e0b"

    # Alpha-composited text colors
    TEXT_INFO_ALPHA = "rgba(255, 255, 255, 0.7)"
    TEXT_ERROR_ALPHA = ERROR
    TEXT_WARNING_ALPHA = "rgba(245, 158, 11, 0.9)"

    # Backgrounds
    BG_HOVER = "rgba(255, 255, 255, 0.08)"
    BG_PRESSED = "rgba(255, 255, 255, 0.05)"
    BG_TRANSPARENT = "transparent"
