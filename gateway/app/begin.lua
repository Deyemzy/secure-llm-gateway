-- reserved -> invoking. Must succeed before Bedrock is called, so a hold the reconciler has
-- already released can never be spent against.
-- KEYS[1] hold record   ARGV[1] now
if redis.call('HGET', KEYS[1], 'state') ~= 'reserved' then return 0 end
if tonumber(redis.call('HGET', KEYS[1], 'deadline')) <= tonumber(ARGV[1]) then return 0 end
redis.call('HSET', KEYS[1], 'state', 'invoking', 'invoked_at', ARGV[1])
return 1
