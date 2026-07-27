import { CanActivate, ExecutionContext, Injectable, Logger, UnauthorizedException } from '@nestjs/common';
import { config } from '../../config/configuration';

/**
 * Guards the /internal/* endpoints the Python worker triggers on a schedule. Auth is a
 * shared secret (INTERNAL_API_KEY) sent in the `x-internal-key` header — not a user JWT,
 * since these are process-to-process calls over the internal Docker network. Fails closed:
 * if no secret is configured on the server, every request is rejected.
 */
@Injectable()
export class InternalKeyGuard implements CanActivate {
  private readonly logger = new Logger(InternalKeyGuard.name);

  canActivate(context: ExecutionContext): boolean {
    const expected = config.internalApiKey;
    if (!expected) {
      this.logger.error('INTERNAL_API_KEY is not set — rejecting internal job request.');
      throw new UnauthorizedException('Internal API not configured');
    }
    const req = context.switchToHttp().getRequest();
    const provided = req.headers['x-internal-key'];
    if (provided !== expected) {
      throw new UnauthorizedException('Invalid internal key');
    }
    return true;
  }
}
