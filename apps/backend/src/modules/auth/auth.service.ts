import { Injectable, UnauthorizedException, ConflictException, BadRequestException, Logger } from '@nestjs/common';
import { JwtService } from '@nestjs/jwt';
import { InjectRepository } from '@nestjs/typeorm';
import { MoreThan, Repository } from 'typeorm';
import * as bcrypt from 'bcryptjs';
import * as crypto from 'crypto';
import { User } from '../../entities/user.entity';
import { RegisterDto, LoginDto, ForgotPasswordDto, ResetPasswordDto } from './dto/auth.dto';

@Injectable()
export class AuthService {
  private readonly logger = new Logger(AuthService.name);
  private static readonly RESET_TTL_MS = 30 * 60 * 1000; // 30 minutes

  constructor(
    @InjectRepository(User)
    private readonly userRepo: Repository<User>,
    private readonly jwtService: JwtService,
  ) {}

  async register(dto: RegisterDto) {
    const existing = await this.userRepo.findOne({ where: { email: dto.email } });
    if (existing) {
      throw new ConflictException('Email already registered');
    }

    const salt = await bcrypt.genSalt(10);
    const passwordHash = await bcrypt.hash(dto.password, salt);

    const user = this.userRepo.create({
      email: dto.email,
      passwordHash,
    });
    await this.userRepo.save(user);

    const token = this.generateToken(user);
    return { user: { id: user.id, email: user.email }, token };
  }

  async login(dto: LoginDto) {
    const user = await this.userRepo.findOne({ where: { email: dto.email } });
    if (!user) {
      throw new UnauthorizedException('Invalid credentials');
    }

    const isValid = await bcrypt.compare(dto.password, user.passwordHash);
    if (!isValid) {
      throw new UnauthorizedException('Invalid credentials');
    }

    const token = this.generateToken(user);
    return { user: { id: user.id, email: user.email }, token };
  }

  /**
   * Begin a password reset. Always resolves the same way regardless of whether the email
   * exists, so the endpoint can't be used to enumerate accounts. When a user does match, a
   * single-use token is generated; its SHA-256 hash + a 30-min expiry are stored on the row.
   * Delivery is first-party: outside production the raw reset link is returned (and logged)
   * so the flow works with no mail provider; wire SMTP here if/when one is available.
   */
  async forgotPassword(dto: ForgotPasswordDto) {
    const user = await this.userRepo.findOne({ where: { email: dto.email } });
    if (!user) {
      return { ok: true };
    }

    const rawToken = crypto.randomBytes(32).toString('hex');
    user.passwordResetTokenHash = crypto.createHash('sha256').update(rawToken).digest('hex');
    user.passwordResetExpiresAt = new Date(Date.now() + AuthService.RESET_TTL_MS);
    await this.userRepo.save(user);

    const frontendUrl = process.env.FRONTEND_URL || 'http://localhost:4321';
    const resetLink = `${frontendUrl}/reset-password?token=${rawToken}`;
    this.logger.log(`Password reset requested for ${user.email}: ${resetLink}`);

    // In production, never return the link in the response body (would leak the token to
    // anyone who can hit the endpoint). Return it only in dev so the flow is testable.
    if (process.env.NODE_ENV === 'production') {
      return { ok: true };
    }
    return { ok: true, resetLink };
  }

  /** Consume a reset token (matched by its SHA-256 hash + non-expired) and set the new password. */
  async resetPassword(dto: ResetPasswordDto) {
    const tokenHash = crypto.createHash('sha256').update(dto.token).digest('hex');
    const user = await this.userRepo.findOne({
      where: { passwordResetTokenHash: tokenHash, passwordResetExpiresAt: MoreThan(new Date()) },
    });
    if (!user) {
      throw new BadRequestException('Reset link is invalid or has expired — request a new one.');
    }

    const salt = await bcrypt.genSalt(10);
    user.passwordHash = await bcrypt.hash(dto.password, salt);
    user.passwordResetTokenHash = null;
    user.passwordResetExpiresAt = null;
    await this.userRepo.save(user);

    const token = this.generateToken(user);
    return { user: { id: user.id, email: user.email }, token };
  }

  private generateToken(user: User): string {
    const payload = { sub: user.id, email: user.email };
    return this.jwtService.sign(payload);
  }
}
