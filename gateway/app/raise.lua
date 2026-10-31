-- Raise a budget counter to at least ARGV[1]; never lower it. Used by the rebuild job when the
-- ledger comes back lower than spend recorded in the audit logs (e.g. after losing the node).
-- KEYS[1] budget counter   ARGV[1] floor (micro-USD)   ARGV[2] ttl s
-- Returns the amount added (0 if the counter was already >= floor).
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
local floor = tonumber(ARGV[1])
if floor <= cur then return 0 end
redis.call('SET', KEYS[1], floor)
redis.call('EXPIRE', KEYS[1], ARGV[2])
return floor - cur
