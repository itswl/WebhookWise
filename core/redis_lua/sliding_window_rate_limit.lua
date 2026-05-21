local prefix = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local current_window = math.floor(now / window)
local previous_window = current_window - 1

local current_key = prefix .. ":" .. current_window
local previous_key = prefix .. ":" .. previous_window

local current_count = tonumber(redis.call("GET", current_key) or "0")
local previous_count = tonumber(redis.call("GET", previous_key) or "0")

local elapsed = now - current_window * window
local weight = (window - elapsed) / window
local estimated = math.floor(previous_count * weight + current_count)

if estimated < limit then
    redis.call("INCR", current_key)
    redis.call("EXPIRE", current_key, window * 2)
    return limit - estimated - 1
else
    return -1
end
