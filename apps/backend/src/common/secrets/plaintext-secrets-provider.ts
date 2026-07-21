import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { ProviderCredential } from '../../entities/provider-credential.entity';
import { SecretsProvider } from './secrets-provider.interface';

@Injectable()
export class PlaintextSecretsProvider implements SecretsProvider {
  constructor(
    @InjectRepository(ProviderCredential)
    private readonly credentialRepo: Repository<ProviderCredential>,
  ) {}

  async getCredential(providerId: string): Promise<Record<string, any> | null> {
    const cred = await this.credentialRepo.findOne({ where: { providerId } });
    return cred ? cred.credentialJson : null;
  }

  async storeCredential(providerId: string, credentialJson: Record<string, any>): Promise<void> {
    let cred = await this.credentialRepo.findOne({ where: { providerId } });
    if (!cred) {
      cred = this.credentialRepo.create({ providerId, credentialJson });
    } else {
      cred.credentialJson = credentialJson;
    }
    await this.credentialRepo.save(cred);
  }

  async deleteCredential(providerId: string): Promise<void> {
    await this.credentialRepo.delete({ providerId });
  }
}
