export interface SecretsProvider {
  getCredential(providerId: string): Promise<Record<string, any> | null>;
  storeCredential(providerId: string, credentialJson: Record<string, any>): Promise<void>;
  deleteCredential(providerId: string): Promise<void>;
}
