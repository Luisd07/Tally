# App icon

Sources for `ios/Tally/Assets.xcassets/AppIcon.appiconset`: tally marks with a slash, on the app's blue.

- `AppIcon.svg`: the standard icon. Exported without an alpha channel (App Store requirement).
- `AppIcon-Dark.svg`: dark appearance (iOS 18+). No background, because iOS supplies it.
- `AppIcon-Tinted.svg`: tinted appearance (iOS 18+). Grayscale, recolored by iOS.

Each exports at 1024×1024. iOS applies the rounded-corner mask, so the artwork is full-bleed.
