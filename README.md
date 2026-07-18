# ha-ecovacs-custom

Custom Ecovacs integration focused on X11 and N20 behavior.

## What this fork adds

This fork adds map-based room sensors on top of upstream Ecovacs behavior:

- Current room sensor for the robot location
- Station current room sensor for the dock/charger location

Both sensors use map room polygons and live position events.

## Custom sensors

### Current room

- Entity key: `current_room`
- Purpose: resolve the active robot position into a room name
- Icon: `mdi:map-marker`

Attributes:

- `x`, `y`, `angle`
- `room_id`
- `map_id`
- `available_room_ids`
- `available_rooms` (name -> id)

### Station current room

- Entity key: `station_current_room`
- Purpose: resolve dock/charger position into a room name
- Icon: `mdi:map-marker`

Attributes:

- `x`, `y`, `angle`
- `room_id`
- `map_id`
- `available_room_ids`
- `available_rooms` (name -> id)

## Model behavior notes (X11 vs N20)

- N20 typically publishes room metadata consistently.
- X11 may publish positions without immediately publishing room metadata.
- This fork includes a guarded one-time rooms refresh fallback when positions arrive but room data is missing.

If `available_room_ids` stays empty on X11, that means room metadata still was not returned for the current session/map.

## Ecovacs login verification

- Ecovacs may now require a one-time email verification code for the integration device ID.
- This fork persists the Ecovacs `deviceId` and prompts for the emailed verification code when Ecovacs returns error `1013`.

## Updating from upstream

Use:

```bash
./update_ecovacs.sh
```

This script pulls upstream Ecovacs integration files into `custom_components/ecovacs` and bumps the local manifest version.
