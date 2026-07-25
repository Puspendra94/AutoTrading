import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { ProviderCredential } from '../../entities/provider-credential.entity';
import { NetworkAvailability, SecretsProvider } from './secrets-provider.interface';

/**
 * Credentials are network-scoped: credential_json holds separate mainnet/testnet slots
 * ({ mainnet: {apiKey, apiSecret}, testnet: {apiKey, apiSecret} }) so a single provider can
 * hold both key sets and the header network toggle switches which one trading/account calls
 * use. A legacy flat { apiKey, apiSecret } (pre-toggle) is still honored on read and is
 * treated as the mainnet slot when new keys are added.
 */
function isFlat(json: any): boolean {
  return !!json && typeof json.apiKey === 'string';
}

function slotFor(useTestnet?: boolean): 'mainnet' | 'testnet' {
  return useTestnet ? 'testnet' : 'mainnet';
}

@Injectable()
export class PlaintextSecretsProvider implements SecretsProvider {
  constructor(
    @InjectRepository(ProviderCredential)
    private readonly credentialRepo: Repository<ProviderCredential>,
  ) {}

  async getCredential(providerId: string, useTestnet?: boolean): Promise<Record<string, any> | null> {
    const cred = await this.credentialRepo.findOne({ where: { providerId } });
    if (!cred?.credentialJson) return null;
    const json = cred.credentialJson as any;
    // Legacy flat keys = mainnet only; never hand them to a testnet request.
    if (isFlat(json)) return useTestnet ? null : json;
    const slot = json[slotFor(useTestnet)];
    return slot?.apiKey && slot?.apiSecret ? slot : null;
  }

  async storeCredential(providerId: string, credentialJson: Record<string, any>, useTestnet?: boolean): Promise<void> {
    let cred = await this.credentialRepo.findOne({ where: { providerId } });
    const existing = (cred?.credentialJson as any) || {};
    // Migrate a legacy flat blob into the mainnet slot before layering in the new network.
    const base = isFlat(existing) ? { mainnet: existing } : { ...existing };
    base[slotFor(useTestnet)] = { apiKey: credentialJson.apiKey, apiSecret: credentialJson.apiSecret };

    if (!cred) {
      cred = this.credentialRepo.create({ providerId, credentialJson: base });
    } else {
      cred.credentialJson = base;
    }
    await this.credentialRepo.save(cred);
  }

  async getConfiguredNetworks(providerId: string): Promise<NetworkAvailability> {
    const cred = await this.credentialRepo.findOne({ where: { providerId } });
    const json = cred?.credentialJson as any;
    if (!json) return { mainnet: false, testnet: false };
    if (isFlat(json)) return { mainnet: true, testnet: false }; // legacy = mainnet only
    return {
      mainnet: !!(json.mainnet?.apiKey && json.mainnet?.apiSecret),
      testnet: !!(json.testnet?.apiKey && json.testnet?.apiSecret),
    };
  }

  async deleteCredential(providerId: string): Promise<void> {
    await this.credentialRepo.delete({ providerId });
  }
}
