# SPDX-License-Identifier: AGPL-3.0-only
"""Native media-Python process only. Enumerate sources without opening capture."""
import hashlib
import json
import sys


def enumerate_devices():
    from gi.repository import Gst
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter('Video/Source', None)
    monitor.add_filter('Audio/Source', None)
    if not monitor.start():
        raise RuntimeError('Windows capture discovery unavailable; check the installed media runtime')
    result = []
    try:
        for device in monitor.get_devices():
            element = device.create_element(None)
            if element is None:
                continue
            factory = element.get_factory().get_name()
            allowed = {'mfvideosrc', 'ksvideosrc', 'dshowvideosrc', 'wasapi2src', 'wasapisrc'}
            if factory not in allowed:
                continue
            props = {}
            for key in ('device-path', 'device', 'device-name', 'device-index'):
                if element.find_property(key):
                    value = element.get_property(key)
                    if isinstance(value, (str, int)) and not isinstance(value, bool):
                        props[key] = value
            if props.get('device-path'):
                props.pop('device-index', None)  # Keep the explicit endpoint, not a volatile index.
            if factory in {'wasapi2src', 'wasapisrc'}:
                endpoint = str(props.get('device', ''))
                # WASAPI also advertises render/loopback and dynamic default endpoints.
                # Only an explicit capture endpoint identifies the chosen microphone.
                if not endpoint.startswith('{0.0.1.'):
                    continue
            properties = device.get_properties()
            identity = properties.to_string() if properties else device.get_display_name()
            fingerprint = hashlib.sha256((factory+'\n'+identity).encode()).hexdigest()
            result.append({'id': fingerprint, 'label': device.get_display_name(),
                           'kind': 'video' if 'Video' in device.get_device_class() else 'audio',
                           'factory': factory, 'properties': props})
    finally:
        monitor.stop()
    return result


def resolve_device(devices, identity, kind):
    matches = [d for d in devices if d['id'] == identity and d['kind'] == kind]
    if len(matches) != 1:
        raise ValueError(f'Selected {kind} device is missing or ambiguous; reopen input setup')
    return matches[0]


if __name__ == '__main__':
    sys.path.insert(0, sys.argv[1])
    from civiccast.native.gstreamer_runtime import bootstrap_installed_gstreamer_runtime
    bootstrap_installed_gstreamer_runtime()
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init(None)
    print(json.dumps(enumerate_devices()), flush=True)
