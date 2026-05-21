# Testing Clyde

Quick guide for testing the app with a mock API.

## Mock API Server (Python)

If you don't have a backend yet, here's a simple Flask server that implements the OpenAI-compatible API:

```python
# mock_api.py
from flask import Flask, request, Response
import json
import time

app = Flask(__name__)

@app.route('/v1/models', methods=['GET'])
def models():
    return json.dumps({
        "data": [{"id": "clyde", "object": "model"}]
    })

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    data = request.json
    is_streaming = data.get('stream', False)
    
    if is_streaming:
        return Response(stream_response(), mimetype='text/event-stream')
    else:
        return json.dumps({
            "choices": [{"message": {"content": "Hello from mock API!"}}]
        })

def stream_response():
    # Simulate thinking
    yield 'data: {"choices":[{"delta":{"content":"<think>Let me consider this question...</think>"}}]}\n\n'
    time.sleep(0.5)
    
    # Simulate tool call
    yield 'data: {"choices":[{"delta":{"content":"*using search...*"}}]}\n\n'
    time.sleep(0.5)
    
    # Stream the actual response
    response = "Sure! I'd be happy to help. This is a **streaming** response with `code` and more content."
    
    for char in response:
        yield f'data: {json.dumps({"choices":[{"delta":{"content":char}}]})}\n\n'
        time.sleep(0.02)  # Simulate typing speed
    
    # Send code block
    code_block = "\n\n```swift\nfunc greet() {\n    print(\"Hello, Clyde!\")\n}\n```\n\n"
    for char in code_block:
        yield f'data: {json.dumps({"choices":[{"delta":{"content":char}}]})}\n\n'
        time.sleep(0.01)
    
    # Final message
    final = "Hope this helps!"
    for char in final:
        yield f'data: {json.dumps({"choices":[{"delta":{"content":char}}]})}\n\n'
        time.sleep(0.02)
    
    # Signal completion
    yield 'data: [DONE]\n\n'

if __name__ == '__main__':
    app.run(port=8801, debug=True)
```

### Running the Mock Server

1. Save the code above as `mock_api.py`
2. Install Flask: `pip install flask`
3. Run: `python mock_api.py`
4. The server will start at `http://localhost:8801`

## Testing Checklist

### Basic Functionality
- [ ] App launches without errors
- [ ] Can create a new conversation (⌘N)
- [ ] Can type and send a message (Enter)
- [ ] Streaming response appears character-by-character
- [ ] Connection status shows green when server is running
- [ ] Connection status shows red when server is stopped

### Message Rendering
- [ ] User messages appear on the right
- [ ] Assistant messages appear on the left
- [ ] Markdown **bold** renders correctly
- [ ] Markdown *italic* renders correctly
- [ ] Inline `code` renders correctly
- [ ] Code blocks show with proper formatting
- [ ] Copy button works on code blocks
- [ ] Timestamps appear below messages

### Special Features
- [ ] `<think>` tags create a collapsible thinking section
- [ ] Thinking section shows animated dots
- [ ] Tool call markers (`*using X...*`) show as pills
- [ ] Tool icons change based on tool name
- [ ] Streaming cursor blinks during message generation

### Attachments
- [ ] Can click paperclip to open file picker
- [ ] Can drag-and-drop image into input area
- [ ] Attachment preview shows before sending
- [ ] Can remove attachment with X button
- [ ] Attached images appear in conversation
- [ ] Multiple attachments work

### Conversation Management
- [ ] Conversation title auto-generates from first message
- [ ] Can search conversations
- [ ] Can pin/unpin conversations
- [ ] Pinned conversations appear in separate section
- [ ] Can rename conversation
- [ ] Can delete conversation
- [ ] Conversations persist after app restart

### Settings
- [ ] Settings sheet opens from gear icon
- [ ] Can change API endpoint
- [ ] Can adjust temperature slider
- [ ] Can adjust max tokens slider
- [ ] Test Connection button works
- [ ] Settings persist after app restart

### Keyboard Shortcuts
- [ ] ⌘N creates new conversation
- [ ] Enter (plain) sends message
- [ ] ⌘Enter / Shift+Enter creates new line in input

### Performance
- [ ] Smooth scrolling in message list
- [ ] Smooth animations (bubbles, thinking dots, etc.)
- [ ] No lag during streaming
- [ ] App remains responsive during streaming
- [ ] Stop button stops streaming immediately

## Common Issues

### Connection Failed
**Problem**: Red dot, "Offline" status  
**Solution**: 
1. Check if mock server is running
2. Verify endpoint in Settings is `http://localhost:8801`
3. Check Console.app for network errors

### No Streaming
**Problem**: Message appears all at once instead of streaming  
**Solution**: 
1. Ensure server is sending SSE format (`data: {json}\n\n`)
2. Check server is setting `stream: true` in response
3. Verify Content-Type is `text/event-stream`

### Markdown Not Rendering
**Problem**: Raw markdown appears instead of formatted text  
**Solution**: This is expected for complex markdown. Phase 1 supports:
- Bold: `**text**`
- Italic: `*text*` or `_text_`
- Code: `` `code` ``
- Code blocks: ` ```lang\ncode\n``` `

### Attachments Not Working
**Problem**: Can't attach files or images don't show  
**Solution**:
1. Ensure file permissions allow reading
2. Check file size (very large files may cause issues)
3. Verify image format is PNG or JPEG

### Conversations Not Saving
**Problem**: Conversations disappear after restart  
**Solution**:
1. Check file permissions for `~/Library/Application Support/`
2. Look for JSON files in `~/Library/Containers/Shastasia.Clyde/Data/Library/Application Support/Clyde/conversations/`
3. Check Console.app for persistence errors

## Advanced Testing

### Test Multimodal
1. Create new conversation
2. Drag an image into the input
3. Type "What's in this image?"
4. Send and check if base64 data is sent to API

### Test Error Handling
1. Stop the mock server
2. Try to send a message
3. Verify error is displayed gracefully
4. Restart server and try again

### Test Large Conversations
1. Send 20+ messages back and forth
2. Verify scrolling is smooth
3. Check app memory usage
4. Restart app and verify conversation loads correctly

### Test Tool Calls
Modify mock server to return:
```python
yield 'data: {"choices":[{"delta":{"content":"*using search...*"}}]}\n\n'
yield 'data: {"choices":[{"delta":{"content":"*using file_reader...*"}}]}\n\n'
yield 'data: {"choices":[{"delta":{"content":"*using web_browser...*"}}]}\n\n'
```

Verify different icons appear (🔍, 📁, 🌐).

### Test Thinking
Modify mock server to return:
```python
yield 'data: {"choices":[{"delta":{"content":"<think>This is my internal reasoning process. I need to analyze the question carefully and consider multiple approaches.</think>"}}]}\n\n'
```

Verify thinking section appears and is collapsible.

## Debugging Tips

### Enable Verbose Logging
In Xcode:
1. Go to Product > Scheme > Edit Scheme
2. Add environment variable: `OS_ACTIVITY_MODE = debug`
3. Check Console for detailed logs

### Inspect Network Traffic
Use Charles Proxy or Proxyman to inspect SSE traffic:
1. Install proxy tool
2. Configure macOS to use proxy
3. Watch requests to `localhost:8801`

### Check Persistence
```bash
# View saved conversations (sandboxed app path)
ls -la ~/Library/Containers/Shastasia.Clyde/Data/Library/Application\ Support/Clyde/conversations/

# View a conversation file
cat ~/Library/Containers/Shastasia.Clyde/Data/Library/Application\ Support/Clyde/conversations/*.json | jq

# Check UserDefaults
defaults read Shastasia.Clyde
```

## Performance Benchmarks

Expected performance:
- **App launch**: < 1 second
- **New conversation**: < 100ms
- **Message send**: < 50ms (before network)
- **Streaming latency**: < 100ms per chunk
- **Scroll FPS**: 60fps
- **Memory usage**: < 100MB for normal conversations

---

Happy testing! 🎉
