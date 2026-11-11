# AutoBase (personal-use prototype)

Android app that watches the screen and taps three buttons you choose
(collect, help, upgrade) so a base can progress overnight.

## How it works
- `AccessibilityService.takeScreenshot()` grabs the screen (Android 11+).
- OpenCV template matching finds your cropped button images.
- `dispatchGesture()` taps the match, with random jitter and delays.
- A 1x1 invisible overlay keeps the screen on.
- Safety: auto-stop after 10 h, stop if the same spot is tapped 6 times in a row,
  optional "only tap while this game package is in front".

## Build (no PC needed)
Push to GitHub. The workflow `Build AutoBase APK` builds the APK and attaches it to a
pre-release named `autobase-build-N`. Open the Releases page on your phone and install it.

## First run
1. Install the APK, open AutoBase.
2. Enable the accessibility service (Step 1 button).
3. In the game take a screenshot of each button, crop only the button, pick it (Step 2).
4. Press START, switch to the game, plug in the charger.

## Notes
- Keep screen timeout at maximum; disable battery optimisation for the app.
- Automating a game may violate its terms of service and can get an account banned.
  Use at your own risk, ideally on a spare account first.
