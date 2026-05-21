# Running Clyde

## ⚠️ Important: Xcode Previews Disabled

All Xcode Previews have been temporarily disabled due to compatibility issues with the preview system in this version of Xcode. This is a known issue and doesn't affect the app's functionality.

## How to Run the App

### Method 1: Run the Full App (Recommended)

1. **Open the project** in Xcode
2. **Select your Mac** as the run destination (top toolbar)
3. **Press ⌘R** or click the Run button
4. The app will launch as a full macOS application

This is the best way to test all features, including:
- Navigation between conversations
- Message streaming
- File attachments
- Settings panel
- Keyboard shortcuts

### Method 2: Build Only

If you just want to make sure everything compiles:

1. Press **⌘B** or choose **Product > Build**
2. Wait for the build to complete
3. Check for any errors in the issue navigator

## First Launch

When you first run the app:

1. You'll see the **"Welcome to Clyde"** empty state
2. Click **"New Conversation"** or press **⌘N**
3. The chat interface will appear
4. Type a message and press **Enter** to send

## Testing with a Local API

Before you can chat, you need an API server running. See **TESTING.md** for instructions on:
- Setting up the Python mock server
- Testing with your own OpenAI-compatible API
- Verifying the connection (green dot = connected)

## Quick Test Checklist

Once the app is running:

- [ ] Create a new conversation (⌘N)
- [ ] Check connection status (bottom right of input area)
- [ ] Type a message and send (Enter)
- [ ] Open settings (gear icon in sidebar)
- [ ] Configure API endpoint if needed
- [ ] Try attaching an image (drag & drop or 📎 button)
- [ ] Pin/unpin a conversation
- [ ] Search for conversations

## Troubleshooting

### App Won't Build

**Check for missing files:**
- Make sure all `.swift` files are in the project
- Required files: ClydeApp.swift, ContentView.swift, Models.swift, AppViewModel.swift, PersistenceManager.swift, APIService.swift, SidebarView.swift, ChatView.swift, MessageBubbleView.swift, SettingsView.swift

**Clean build folder:**
1. Product > Clean Build Folder (⇧⌘K)
2. Quit Xcode
3. Delete DerivedData: `~/Library/Developer/Xcode/DerivedData/Clyde-*`
4. Reopen Xcode and build again

### Red Connection Dot

The connection indicator will be red until you:
1. Start a local API server (see TESTING.md)
2. Configure the correct endpoint in Settings
3. Make sure the server is running on the expected port (default: 8801)

### Messages Won't Send

Make sure:
- API server is running (green dot)
- You've typed a message (not just whitespace)
- You're not currently streaming (stop button not showing)

### Keyboard Shortcuts Not Working

- Make sure the app has focus (click on the window)
- ⌘N requires the app to be active
- Enter requires the text input to have focus
- ⌘Enter / Shift+Enter inserts a newline

## Performance Notes

Expected performance on modern Macs:
- **App launch:** < 1 second
- **Build time:** 5-10 seconds (first build may take longer)
- **Message rendering:** Instant
- **Streaming:** Smooth, no lag

If you experience slowness:
- Check Activity Monitor for high CPU usage
- Make sure you're running on Apple Silicon (native) or Intel
- Try reducing the number of messages in a conversation

## Next Steps

Once the app is running successfully:

1. **Test basic features** (see Quick Test Checklist above)
2. **Set up your API** (see TESTING.md for mock server)
3. **Customize settings** (theme, temperature, etc.)
4. **Explore features** (attachments, tool calls, thinking sections)
5. **Read EXTENDING.md** to add your own features

## Getting Help

If you encounter issues:

1. **Check the console** in Xcode (⇧⌘C) for error messages
2. **Look at ~/Library/Logs/DiagnosticReports** for crash logs
3. **Review TESTING.md** for common problems and solutions
4. **Check ARCHITECTURE.md** to understand the code structure

---

**Note:** Xcode Previews will be re-enabled once the compatibility issues are resolved. For now, running the full app (⌘R) is the recommended testing approach.
