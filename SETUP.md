# Setting Up Your CSEC AI Study Partner

This guide gets your study app running on a Windows laptop. You only do the full
setup **once**. After that, you just plug in the SSD and click one file.

Take your time and follow the steps in order. 

## What you need

- A **Windows laptop**.
- Your **CSEC Study Partner SSD** (the small external drive).
- **Internet** — only for the first setup, to download the AI. After that the app
  works fully offline.
- About **30 minutes** (most of it is waiting for downloads).

---

## Steps

1. **Plug in the SSD.** Open **File Explorer** and look on the left for the drive.
   Write down its **letter** — it will be something like `D:` or `E:`. You'll need
   it later.

2. **Install Python.** Go to **https://python.org/downloads** and click the big
   download button. Open the file you downloaded. **VERY IMPORTANT:** on the first
   screen, tick the box that says **"Add Python to PATH"** before you click
   *Install Now*. If you miss that box, the app won't work.

3. **Install Ollama** (this is the AI engine). Go to
   **https://ollama.com/download**, download it, and install it like any normal
   program.

4. **Open the SSD** in File Explorer, open the **`launch`** folder, and
   **double-click `setup.bat`**. A black window opens and starts working. Leave it
   open.

5. **If the window says it created a `.env` file:** it assumes your SSD is drive
   `D:`. If your drive letter from Step 1 is *different*, open the `.env` file
   (right-click → *Open with* → *Notepad*), change every `D:` to your letter, save,
   and double-click `setup.bat` again.

6. **If the window asks you to set `OLLAMA_MODELS`:** copy the line it shows you,
   paste it into **PowerShell** (search "PowerShell" in the Start menu), press
   Enter, then double-click `setup.bat` again. This keeps the big AI files on the
   SSD instead of filling up the laptop.

7. **Wait for the models to download.** This is the long part — about **10 to 20
   minutes** depending on your internet. The window shows progress bars. **Don't
   close it.**

8. **Watch for `Setup complete!`** When you see that message, the hard part is
   done. You never have to do Steps 2–7 again on this laptop.

9. **Start studying:** in the `launch` folder, **double-click `start.bat`**. A
   black window opens and gets everything ready.

10. **Your web browser opens by itself** and shows the study app. **Pick your
    subject** and start studying.

11. **When you're finished:** close the black window — click the **X** in its
    top-right corner. That stops the study app right away. Wait a couple of
    seconds, then it's safe to unplug the SSD.

---

## Troubleshooting

**It says "SSD not found."**
Plug the SSD back in, and make sure its drive letter matches the one in your
`.env` file (see Steps 1 and 5), then try again.

**It says "Ollama not found."**
Install Ollama from **https://ollama.com/download**, then double-click `setup.bat`
again.

**The browser doesn't open.**
Open your web browser yourself and go to **http://127.0.0.1:8000**.

**It says "Python not found" or pip errors.**
You probably missed the **"Add Python to PATH"** checkbox in Step 2. Reinstall
Python and make sure that box is ticked.

**Still stuck?** Close everything, unplug and replug the SSD, and run `setup.bat`
once more — most problems fix themselves on a clean retry.
