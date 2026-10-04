// grokbot-things-confirm: native, fail-safe approval for one Things change.
//
// Reads a JSON object on stdin:
//   {"client_name": str, "headline": str,
//    "rows": [[label, value], ...], "details": str}
// and shows it in a modal alert. "Cancel" is the default button (Return);
// "Apply" must be clicked deliberately. The dialog times out after 60 seconds.
//
// Exit status: 0 = Apply, 1 = Cancel, 2 = invalid input, 3 = timed out.

#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#include <float.h>

static const NSTimeInterval kConfirmationTimeoutSeconds = 60.0;
static const NSUInteger kMaxRows = 16;

@interface ConfirmationTimeout : NSObject
@property(nonatomic, assign) BOOL timedOut;
- (void)fire:(NSTimer *)timer;
@end

@implementation ConfirmationTimeout
- (void)fire:(NSTimer *)timer {
    (void)timer;
    self.timedOut = YES;
    [NSApp abortModal];
}
@end

static NSString *RequiredString(NSDictionary *payload, NSString *key) {
    id value = payload[key];
    return [value isKindOfClass:[NSString class]] ? value : nil;
}

int main(void) {
    @autoreleasepool {
        NSData *input = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        NSError *jsonError = nil;
        id decoded = [NSJSONSerialization JSONObjectWithData:input options:0 error:&jsonError];
        if (jsonError != nil || ![decoded isKindOfClass:[NSDictionary class]]) {
            fprintf(stderr, "confirmation helper: invalid JSON input\n");
            return 2;
        }

        NSDictionary *payload = decoded;
        NSString *clientName = RequiredString(payload, @"client_name");
        NSString *headline = RequiredString(payload, @"headline");
        NSString *details = RequiredString(payload, @"details");
        id rows = payload[@"rows"];
        if (clientName.length == 0 || headline.length == 0 || details == nil ||
            ![rows isKindOfClass:[NSArray class]] || [rows count] == 0 ||
            [rows count] > kMaxRows) {
            fprintf(stderr, "confirmation helper: required field missing\n");
            return 2;
        }

        NSMutableArray<NSString *> *lines = [NSMutableArray array];
        for (id row in rows) {
            if (![row isKindOfClass:[NSArray class]] || [row count] != 2 ||
                ![row[0] isKindOfClass:[NSString class]] ||
                ![row[1] isKindOfClass:[NSString class]]) {
                fprintf(stderr, "confirmation helper: malformed row\n");
                return 2;
            }
            [lines addObject:[NSString stringWithFormat:@"%@: %@", row[0], row[1]]];
        }

        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];

        NSAlert *alert = [[NSAlert alloc] init];
        alert.alertStyle = NSAlertStyleWarning;
        alert.messageText = headline;
        alert.informativeText = [lines componentsJoinedByString:@"\n"];

        if (details.length > 0) {
            NSScrollView *scrollView = [[NSScrollView alloc]
                initWithFrame:NSMakeRect(0, 0, 520, 220)];
            scrollView.borderType = NSBezelBorder;
            scrollView.hasVerticalScroller = YES;
            scrollView.hasHorizontalScroller = NO;

            NSTextView *textView = [[NSTextView alloc] initWithFrame:scrollView.bounds];
            textView.editable = NO;
            textView.selectable = YES;
            textView.richText = NO;
            textView.font = [NSFont systemFontOfSize:[NSFont systemFontSize]];
            textView.string = details;
            textView.verticallyResizable = YES;
            textView.horizontallyResizable = NO;
            textView.autoresizingMask = NSViewWidthSizable;
            textView.textContainer.containerSize =
                NSMakeSize(scrollView.contentSize.width, FLT_MAX);
            textView.textContainer.widthTracksTextView = YES;
            scrollView.documentView = textView;
            alert.accessoryView = scrollView;
        }

        NSButton *cancelButton = [alert addButtonWithTitle:@"Cancel"];
        NSButton *applyButton = [alert addButtonWithTitle:@"Apply"];
        cancelButton.keyEquivalent = @"\r";
        applyButton.keyEquivalent = @"";

        ConfirmationTimeout *timeout = [[ConfirmationTimeout alloc] init];
        NSTimer *timer = [NSTimer timerWithTimeInterval:kConfirmationTimeoutSeconds
                                                 target:timeout
                                               selector:@selector(fire:)
                                               userInfo:nil
                                                repeats:NO];
        [[NSRunLoop currentRunLoop] addTimer:timer forMode:NSModalPanelRunLoopMode];

        [NSApp activateIgnoringOtherApps:YES];
        NSModalResponse response = [alert runModal];
        [timer invalidate];

        if (timeout.timedOut) {
            return 3;
        }
        return response == NSAlertSecondButtonReturn ? 0 : 1;
    }
}
