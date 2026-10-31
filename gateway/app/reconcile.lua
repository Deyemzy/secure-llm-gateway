-- Recover a hold whose request never closed it (worker crash, OOM kill, deploy):
--   reserved  -> expired_released : Bedrock was never called, so refund the hold.
--   invoking  -> expired_charged  : Bedrock may have billed; keep the worst-case charge (never
--                                   under-count) and flag for reconciliation against Bedrock
--                                   invocation logs (requestMetadata.gateway_request_id).
-- KEYS[1] team spent  KEYS[2] user spent  KEYS[3] hold record  KEYS[4] deadline index
-- ARGV[1] now  ARGV[2] request id  ARGV[3] released-record ttl  ARGV[4] charged-record ttl
local s = redis.call('HGET', KEYS[3], 'state')
if not s then redis.call('ZREM', KEYS[4], ARGV[2]); return {'gone', 0} end
if s ~= 'reserved' and s ~= 'invoking' then redis.call('ZREM', KEYS[4], ARGV[2]); return {'closed', 0} end
if tonumber(redis.call('HGET', KEYS[3], 'deadline')) > tonumber(ARGV[1]) then return {'not_due', 0} end
local amount = tonumber(redis.call('HGET', KEYS[3], 'amount'))
redis.call('ZREM', KEYS[4], ARGV[2])
if s == 'reserved' then
  redis.call('INCRBY', KEYS[1], -amount)
  redis.call('INCRBY', KEYS[2], -amount)
  redis.call('HSET', KEYS[3], 'state', 'expired_released', 'actual', 0, 'closed_at', ARGV[1])
  redis.call('EXPIRE', KEYS[3], ARGV[3])
  return {'expired_released', amount}
end
redis.call('HSET', KEYS[3], 'state', 'expired_charged', 'closed_at', ARGV[1])
redis.call('EXPIRE', KEYS[3], ARGV[4])
return {'expired_charged', amount}
