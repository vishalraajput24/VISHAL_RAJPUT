# Night Builder (personal-use prototype)

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

## AI mode (optional)
Providers: Google Gemini (free key from AI Studio, default) or Anthropic Claude (paid API key).
Free tiers have small daily quotas and may use your data to improve products; check the provider terms.

Turn on "Use AI mode" in the app and paste your Anthropic API key. Instead of matching saved
button images, the app sends a downscaled screenshot to Claude, which returns one action
(tap a box / wait / stop). Guards in the app: game-package filter is mandatory (other apps are
never sent), blocked words (shop, gems, buy, speed up, VIP, deals, prices) refuse the tap,
stop when the same spot is tapped 4 times, daily call cap, 10 h auto-stop.
Default model is claude-haiku-5-5 (cheap); change it in the Model field for more accurate taps.
The API key is stored only in the app's private storage on the phone.

## Skills (learning)
When the AI completes a routine that is safe to repeat (Help All, collecting, training the same troop), the app
saves it as a skill: the tapped buttons are cropped as small images plus the tap order and a repeat timer.
Due skills are replayed with image matching and no AI call. If a replay step is missing, the app closes the
open menus and asks the AI again; a skill that fails 3 times is disabled and re-learned. The "Skills and
discoveries" button lists what was learned and what the AI noticed it could automate.
