"""Read-only memory measurements for an exact Studio-owned browser process tree."""

from pathlib import Path


def blank_browser(context):
    pages = [page for page in context.pages if not page.is_closed()]
    return len(pages) == 1 and pages[0].url in ('about:blank', 'about:newtab')


def profile_memory(store, profile_id):
    import psutil

    unavailable = {'rssBytes': None, 'processCount': None}
    try:
        expected = store.profile_dir(profile_id).resolve()
        matches = []
        for process in psutil.Process().children(recursive=True):
            try:
                name = process.name().lower()
                if not (name.startswith('camoufox') or name.startswith('firefox')):
                    continue
                args = process.cmdline()
                if '-profile' in args and Path(args[args.index('-profile') + 1]).resolve() == expected:
                    matches.append(process)
            except (psutil.Error, OSError, ValueError, IndexError):
                continue
        if len(matches) != 1:
            return unavailable
        browser = matches[0]
        created = browser.create_time()
        processes = [browser, *browser.children(recursive=True)]
        rss = sum(process.memory_info().rss for process in processes)
        if not 0 < rss < 2 ** 53 or not browser.is_running() or browser.create_time() != created:
            return unavailable
        return {'rssBytes': rss, 'processCount': len(processes)}
    except (psutil.Error, OSError, AttributeError):
        return unavailable
